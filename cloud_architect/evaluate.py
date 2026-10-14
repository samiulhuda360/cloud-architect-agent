"""Evaluate the architects on the same 20 workloads, and the policy extractors on labelled documents.

    python -m cloud_architect eval                     # all systems (needs an LLM for two of them)
    python -m cloud_architect eval --only rules        # no model calls
    python -m cloud_architect eval --only extraction   # policy extraction: rules always, the model when configured
    python -m cloud_architect eval --rescore           # re-score saved runs, no model calls (what CI does)
    python -m cloud_architect eval --only agent --resume   # re-run only scenarios that hit a provider error

Systems:
  rules      the rule-based architect (no model)
  agent      the LLM with tools: live pricing, constraint checks, the review
  llm_only   the same LLM with the same service menu but no tools; it estimates prices from memory

Every design, whoever made it, is scored by the same deterministic assessor:
  silent violations   hard constraints broken without an accepted exception (residency, budget, coverage, policy)
  within budget       total under the scenario's budget
  expectations met    scenario-specific checks (e.g. primary region near the users, DR in the paired region)
  high findings       Well-Architected and compliance findings of high severity left open
  terraform valid     `terraform validate` passes on the generated code (when Terraform is installed)
  cost error          llm_only only: how far its own estimate is from the live price of its design

Policy extraction is scored against hand-labelled rules (eval/policy_labels.yaml): a proposed rule is
correct when its check type and parameters match a label. Precision = correct / proposed,
recall = correct / labelled. Model replies are saved, so re-scoring never calls a model.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .agent import design_with_tools, design_without_tools
from .assess import assess, dr_gaps, money
from .catalogue import SERVICES
from .config import ROOT, settings
from .models import Design, PolicyRule, Workload
from .pricing import PriceBook
from .regions import RESIDENCY_REGIONS
from .rules import architect
from .terraform import generate

EVAL = ROOT / "eval"
RUNS = EVAL / "runs"
EXAMPLES = ROOT / "examples"
# Infrastructure failures (rate limits, timeouts, 5xx, a used-up daily quota) say nothing about the architect,
# so --resume re-runs them. A model that answers badly is never re-run.
PROVIDER_ERRORS = ("RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError", "RuntimeError: provider", "DailyQuotaError")
TERRAFORM = shutil.which("terraform") or str(Path(os.getenv("TERRAFORM", "")) if os.getenv("TERRAFORM") else "")


def load_scenarios() -> list[tuple[str, Workload, dict]]:
    out = []
    for s in yaml.safe_load((EVAL / "scenarios.yaml").read_text(encoding="utf-8")):
        expect = s.pop("expect", {}) or {}
        sid = s.pop("id")
        out.append((sid, Workload(**s), expect))
    return out


def load_workload(path: Path) -> Workload:
    return Workload(**yaml.safe_load(path.read_text(encoding="utf-8")))


def load_examples() -> list[tuple[str, Workload]]:
    return [(p.parent.name, load_workload(p)) for p in sorted(EXAMPLES.glob("*/workload.yaml"))]


def expectations(d: Design, a, expect: dict, w: Workload) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    services = {c.service for c in d.components}
    tiers = {c.tier for c in d.components}
    stores = [c for c in d.components if c.service in SERVICES and SERVICES[c.service].data_store]
    if "max_total" in expect:
        checks["max_total"] = a.total_month <= expect["max_total"]
    if "region" in expect:
        checks["region"] = d.region == expect["region"]
    if "dr_in" in expect:
        checks["dr_in"] = d.dr_region in expect["dr_in"]
    if "all_in" in expect:
        allowed = set(RESIDENCY_REGIONS[expect["all_in"]]) if isinstance(expect["all_in"], str) else set(expect["all_in"])
        inside = [
            c for c in d.components if c.service in SERVICES and not SERVICES[c.service].global_service and "llm" not in SERVICES[c.service].roles
        ]
        checks["all_in"] = all(c.region in allowed for c in inside)
    if expect.get("private_data"):
        checks["private_data"] = all(c.private_endpoint for c in stores if SERVICES[c.service].private_endpoint_ok)
    if expect.get("zone_redundant_db"):
        checks["zone_redundant_db"] = any(c.zone_redundant for c in d.components if c.service in ("postgres", "azure_sql", "cosmos_db"))
    for s in expect.get("has", []):
        checks[f"has:{s}"] = s in services
    for s in expect.get("no_service", []):
        checks[f"no_service:{s}"] = s not in services
    for t in expect.get("no_tier", []):
        checks[f"no_tier:{t}"] = t not in tiers
    if expect.get("llm_exception"):
        checks["llm_exception"] = any("llm" in e.lower() or "openai" in e.lower() or "res-" in e.lower() for e in d.accepted_exceptions)
    if expect.get("dr_region"):
        checks["dr_region"] = not dr_gaps(d, w)  # a standby and a database copy actually run there
    if expect.get("db_replica"):
        checks["db_replica"] = bool(d.dr_region) and not dr_gaps(d, w, need_compute=False)
    if expect.get("dr_note"):
        checks["dr_note"] = bool(d.dr_region) or any(
            "region" in t.lower() and ("one" in t.lower() or "single" in t.lower() or "dr" in t.lower()) for t in d.tradeoffs + d.accepted_exceptions
        )
    if expect.get("waf"):
        checks["waf"] = "app_gateway" in services or any(c.service == "front_door" and c.tier == "premium" for c in d.components)
    if expect.get("geo_backup"):
        checks["geo_backup"] = any(c.settings.get("geo_backup") or c.settings.get("replica") or c.tier == "hot_grs" for c in d.components)
    if "log_days" in expect:
        checks["log_days"] = any(c.settings.get("retention_days", 30) >= expect["log_days"] for c in d.components if c.service == "log_analytics")
    if expect.get("cmk"):
        checks["cmk"] = all(c.customer_managed_key for c in stores if SERVICES[c.service].private_endpoint_ok and not c.settings.get("replica"))
    if expect.get("autoscale"):
        checks["autoscale"] = any(c.settings.get("max_instances", 0) > c.instances for c in d.components)
    if expect.get("audit"):
        checks["audit"] = any(c.settings.get("audit") for c in d.components if c.service == "log_analytics")
    return checks


TF_DIR = ROOT / "build" / "tf-validate"


def terraform_valid(d: Design, w: Workload, failures: Path | None = None) -> bool | None:
    """`terraform validate` on the generated code. Every design uses the same providers, so one
    working folder is initialised once and reused; Terraform's temp files stay inside build/."""
    if not TERRAFORM:
        return None
    tmp = ROOT / "build" / "tmp"
    for p in (TF_DIR, tmp):
        p.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "TMPDIR": str(tmp),
        "TF_PLUGIN_CACHE_DIR": os.getenv("TF_PLUGIN_CACHE_DIR", str(ROOT / "cache" / "tf-plugins")),
    }
    Path(env["TF_PLUGIN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    code = generate(d, w)
    (TF_DIR / "main.tf").write_text(code, encoding="utf-8")
    steps = [["validate", "-no-color"]]
    if not (TF_DIR / ".terraform").exists():
        steps.insert(0, ["init", "-backend=false", "-input=false", "-no-color"])
    for cmd in steps:
        r = subprocess.run([TERRAFORM, *cmd], cwd=TF_DIR, env=env, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            if failures:
                failures.mkdir(parents=True, exist_ok=True)
                (failures / "main.tf").write_text(code, encoding="utf-8")
                (failures / "validate.log").write_text(r.stdout + r.stderr, encoding="utf-8")
            return False
    return True


def score(sid: str, w: Workload, expect: dict, d: Design | None, book: PriceBook, extra: dict, check_tf: bool) -> dict:
    if d is None:
        return {"id": sid, "ok": False, "error": extra.get("error", "no design"), **{k: v for k, v in extra.items() if k != "error"}}
    a = assess(d, w, book)
    checks = expectations(d, a, expect, w)
    row = {
        "id": sid,
        "ok": True,
        "total": a.total_month,
        "currency": a.currency,
        "silent_violations": [v.rule for v in a.violations],
        "within_budget": w.budget_month is None or a.total_month <= w.budget_month,
        "expectations": checks,
        "expectations_met": all(checks.values()) if checks else True,
        "high_findings": sorted({f.rule for f in a.findings if f.severity == "high" and "Accepted:" not in f.message}),
        "exceptions": d.accepted_exceptions,
        "design": d.model_dump(),
        **extra,
    }
    if extra.get("claimed_total"):
        row["cost_error"] = round(abs(extra["claimed_total"] - a.total_month) / max(a.total_month, 1), 3)
    if check_tf:
        row["terraform_valid"] = terraform_valid(d, w, ROOT / "build" / "tf-failures" / f"{extra.get('system', 'x')}-{sid}")
    return row


def summarise(rows: list[dict]) -> dict:
    done = [r for r in rows if r.get("ok")]
    n = len(rows)
    out = {
        "scenarios": n,
        "designs": len(done),
        "no_silent_violations": sum(not r["silent_violations"] for r in done) / n,
        "within_budget": sum(r["within_budget"] for r in done) / n,
        "expectations_met": sum(r["expectations_met"] for r in done) / n,
        "avg_high_findings": round(statistics.mean(len(r["high_findings"]) for r in done), 2) if done else None,
        "median_seconds": statistics.median(r.get("seconds", 0) for r in done) if done else None,
    }
    tf = [r["terraform_valid"] for r in done if r.get("terraform_valid") is not None]
    if tf:
        out["terraform_valid"] = sum(tf) / len(tf)
    errs = [r["cost_error"] for r in done if "cost_error" in r]
    if errs:
        out["median_cost_error"] = statistics.median(errs)
        out["cost_within_20pct"] = sum(e <= 0.2 for e in errs) / len(errs)
    return out


def run_system(system: str, scenarios, book: PriceBook, cfg, check_tf: bool, pause: float, keep: dict | None = None) -> list[dict]:
    rows, stopped = [], ""
    for sid, w, expect in scenarios:
        if keep and sid in keep:
            rows.append(keep[sid])
            continue
        if stopped:  # the daily quota is gone: record the rest as provider errors so --resume picks them up later
            rows.append({"id": sid, "ok": False, "error": stopped, "system": system})
            continue
        t = time.time()
        try:
            if system == "rules":
                d, extra = architect(w, book), {}
            elif system == "agent":
                r = design_with_tools(w, book, cfg)
                d, extra = r.design, {"steps": len(r.steps), "error": r.error}
            else:
                r = design_without_tools(w, cfg)
                d, extra = r.design, {"steps": len(r.steps), "claimed_total": r.claimed_total, "error": r.error}
        except Exception as exc:  # noqa: BLE001  provider errors are recorded, not fatal
            d, extra = None, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            if type(exc).__name__ == "DailyQuotaError":
                stopped = extra["error"]
                print(f"  {system}: daily quota reached, stopping ({stopped[:120]})", flush=True)
        extra.update({"system": system, "seconds": round(time.time() - t, 1)})
        row = score(sid, w, expect, d, book, extra, check_tf)
        rows.append(row)
        mark = "ok " if row.get("ok") and not row.get("silent_violations") else "!! "
        total = money(row["total"], row["currency"]) if row.get("ok") else "-"
        print(f"  {system:8s} {sid} {mark}{total:>12}  {row.get('silent_violations', row.get('error', ''))}", flush=True)
        if system != "rules" and pause:
            time.sleep(pause)
    return rows


NAMES = {"rules": "Rule-based architect (no model)", "agent": "LLM agent with tools", "llm_only": "LLM alone (no tools)"}


def report(results: dict[str, dict], scenarios) -> str:
    pct = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
    by_id = {sid: w for sid, w, _ in scenarios}
    lines = [
        "# Evaluation",
        "",
        f"{len(scenarios)} workloads in {len({w.residency for w in by_id.values()})} jurisdictions and "
        f"{len({w.currency for w in by_id.values()})} currencies (`eval/scenarios.yaml`), every design scored by the same deterministic assessor.",
        "",
        "| System | Model | No silent violations | Within budget | Expectations met | High findings left (avg) | Terraform valid "
        "| LLM's own cost estimate within 20% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for system, res in results.items():
        s = res["summary"]
        lines.append(
            f"| {NAMES.get(system.split(' ')[0], system)} | {res.get('model') or '-'} | {pct(s['no_silent_violations'])} "
            f"| {pct(s['within_budget'])} | "
            f"{pct(s['expectations_met'])} | {s['avg_high_findings']} | {pct(s.get('terraform_valid'))} | {pct(s.get('cost_within_20pct'))} |"
        )
    lines += [
        "",
        "## Per scenario",
        "",
        "| Scenario | Residency | " + " | ".join(NAMES.get(s.split(" ")[0], s) for s in results) + " |",
        "|---|---|" + "---|" * len(results),
    ]
    for sid, w, _ in scenarios:
        cells = []
        for res in results.values():
            r = next((x for x in res["rows"] if x["id"] == sid), None)
            if not r or not r.get("ok"):
                cells.append("no design" if r else "not run")
                continue
            flags = ("silent: " + ", ".join(r["silent_violations"])) if r["silent_violations"] else "ok"
            missed = [k for k, v in r["expectations"].items() if not v]
            cells.append(f"{money(r['total'], r['currency'])} · {flags}" + (f" · missed {', '.join(missed)}" if missed else ""))
        lines.append(f"| {sid} {w.name} | {w.residency} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- policy extraction
def load_labels() -> dict[str, list[dict]]:
    raw = yaml.safe_load((EVAL / "policy_labels.yaml").read_text(encoding="utf-8"))
    out = {}
    for doc, rules in raw.items():
        labelled = []
        for r in rules:
            r = dict(r)
            if "jurisdiction" in r:
                r["regions"] = sorted(RESIDENCY_REGIONS[r.pop("jurisdiction")])
            labelled.append(r)
        out[doc] = labelled
    return out


def rule_matches(pred: dict, gold: dict, strict: bool = True) -> bool:
    if pred["check"] != gold["check"]:
        return False
    if not strict:
        return True
    if "regions" in gold and set(pred.get("regions") or []) != set(gold["regions"]):
        return False
    if "days" in gold and pred.get("days") != gold["days"]:
        return False
    if "amount" in gold and (abs((pred.get("amount") or 0) - gold["amount"]) > 0.01 * gold["amount"] or pred.get("currency") != gold["currency"]):
        return False
    if "services" in gold and set(pred.get("services") or []) != set(gold["services"]):
        return False
    if "tags" in gold and set(pred.get("tags") or []) != set(gold["tags"]):
        return False
    return True


def score_extraction(predicted: dict[str, list[dict]], labels: dict[str, list[dict]], strict: bool = True) -> dict:
    tp = fp = fn = 0
    for doc, gold in labels.items():
        preds = list(predicted.get(doc, []))
        unmatched = list(gold)
        for p in preds:
            hit = next((g for g in unmatched if rule_matches(p, g, strict)), None)
            if hit:
                unmatched.remove(hit)
                tp += 1
            else:
                fp += 1
        fn += len(unmatched)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"labelled": tp + fn, "proposed": tp + fp, "correct": tp, "precision": round(precision, 3), "recall": round(recall, 3)}


def run_extraction(cfg, methods: list[str]) -> None:
    from .policies import extract_with_model, extract_with_rules

    labels = load_labels()
    for method in methods:
        predicted, dropped = {}, 0
        for doc in labels:
            text = (ROOT / doc).read_text(encoding="utf-8")
            if method == "rules":
                rules = extract_with_rules(text, Path(doc).name)
            else:
                rules, gone = extract_with_model(text, Path(doc).name, cfg)
                dropped += gone
            predicted[doc] = [r.model_dump() for r in rules]
            print(f"  extraction/{method} {doc}: {len(rules)} rules")
        model = "" if method == "rules" else cfg.llm_model
        slug = "extraction-" + method + ("-" + model.replace("/", "_") if model else "")
        (RUNS / f"{slug}.json").write_text(
            json.dumps({"method": method, "model": model, "dropped_ungrounded": dropped, "predicted": predicted}, indent=1), encoding="utf-8"
        )


def extraction_report() -> str:
    labels = load_labels()
    splits = {
        "development (5 documents)": {d: r for d, r in labels.items() if not d.startswith("eval/policies-holdout/")},
        "held-out (3 documents)": {d: r for d, r in labels.items() if d.startswith("eval/policies-holdout/")},
    }
    lines = [
        "",
        "# Policy extraction",
        "",
        f"{sum(len(v) for v in labels.values())} hand-labelled rules in {len(labels)} documents (`eval/policy_labels.yaml`). "
        "A proposed rule is correct "
        "when its check type and parameters match a label (strict); the lenient score only asks for the right check type.",
        "",
        "| Extractor | Documents | Labelled | Proposed | Correct | Precision | Recall | Precision (lenient) | Recall (lenient) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for path in sorted(RUNS.glob("extraction-*.json")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        name = "Rule-based extractor (no model)" if saved["method"] == "rules" else f"Model ({saved['model']})"
        for split, gold in splits.items():
            preds = {d: [PolicyRule.model_validate(r).model_dump() for r in saved["predicted"].get(d, [])] for d in gold}
            s, loose = score_extraction(preds, gold), score_extraction(preds, gold, strict=False)
            lines.append(
                f"| {name} | {split} | {s['labelled']} | {s['proposed']} | {s['correct']} | {s['precision']:.0%} | {s['recall']:.0%} "
                f"| {loose['precision']:.0%} | {loose['recall']:.0%} |"
            )
        if saved.get("dropped_ungrounded"):
            lines.append(f"\n{name}: {saved['dropped_ungrounded']} proposed rule(s) dropped because their quote is not in the document.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    cfg = settings()
    book = PriceBook(cfg.price_cache)
    scenarios = load_scenarios()
    RUNS.mkdir(parents=True, exist_ok=True)
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else ["rules", "agent", "llm_only", "extraction"]
    check_tf = "--no-terraform" not in argv
    pause = float(os.getenv("EVAL_PAUSE", "0"))
    if "--rescore" not in argv:
        for system in only:
            if system == "extraction":
                print("\nextraction:")
                run_extraction(cfg, ["rules"] + (["model"] if cfg.llm_configured else []))
                continue
            if system != "rules" and not cfg.llm_configured:
                print(f"skipping {system}: no LLM configured")
                continue
            print(f"\n{system}:")
            model = "" if system == "rules" else cfg.llm_model
            slug = system + ("-" + model.replace("/", "_").replace(":", "_") if model else "")
            keep = None
            if "--resume" in argv and (RUNS / f"{slug}.json").exists():
                saved = json.loads((RUNS / f"{slug}.json").read_text(encoding="utf-8"))["rows"]
                keep = {r["id"]: r for r in saved if r.get("ok") or not str(r.get("error", "")).startswith(PROVIDER_ERRORS)}
            rows = run_system(system, scenarios, book, cfg, check_tf, pause, keep)
            (RUNS / f"{slug}.json").write_text(json.dumps({"system": system, "model": model, "rows": rows}, indent=1), encoding="utf-8")
    results = {}
    by_id = {sid: (w, e) for sid, w, e in scenarios}
    for path in sorted(p for p in RUNS.glob("*.json") if not p.name.startswith("extraction-")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        rows = []
        for r in saved["rows"]:
            if r["id"] not in by_id:
                continue
            w, e = by_id[r["id"]]
            d = Design.model_validate(r["design"]) if r.get("design") else None
            keep = {k: r[k] for k in ("system", "seconds", "steps", "claimed_total", "error") if k in r}
            new = score(r["id"], w, e, d, book, keep, check_tf=False)
            if "terraform_valid" in r:
                new["terraform_valid"] = r["terraform_valid"]
            rows.append(new)
        key = saved["system"] if saved["system"] not in results else f"{saved['system']} ({saved['model']})"
        results[key] = {"model": saved["model"], "rows": rows, "summary": summarise(rows)}
        path.write_text(json.dumps({**saved, "rows": rows}, indent=1), encoding="utf-8")
    order = {"rules": 0, "llm_only": 1, "agent": 2}
    results = dict(sorted(results.items(), key=lambda kv: order.get(kv[0].split(" ")[0], 9)))
    (EVAL / "results.md").write_text(report(results, scenarios) + extraction_report(), encoding="utf-8")
    for system, res in results.items():
        print(system, json.dumps(res["summary"]))
    print("wrote eval/results.md")


if __name__ == "__main__":
    main()

"""Evaluate three architects on the same 20 workloads.

    python -m cloud_architect eval                 # all systems (needs an LLM for two of them)
    python -m cloud_architect eval --only rules    # no model calls
    python -m cloud_architect eval --rescore       # re-score saved runs, no model calls
    python -m cloud_architect eval --only agent --resume   # re-run only scenarios that hit a provider error

Systems:
  rules      the rule-based architect (no model)
  agent      the LLM with tools: live pricing, constraint checks, the review
  llm_only   the same LLM with the same service menu but no tools; it estimates prices from memory

Every design, whoever made it, is scored by the same deterministic assessor:
  silent violations   hard constraints broken without an accepted exception (residency, budget, coverage)
  within budget       total under the scenario's budget
  expectations met    scenario-specific checks (e.g. data stays in NZ North, no Global AI deployment)
  high findings       Well-Architected findings of high severity left open
  terraform valid     `terraform validate` passes on the generated code (when Terraform is installed)
  cost error          llm_only only: how far its own estimate is from the live price of its design
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
from .assess import assess, dr_gaps
from .catalogue import SERVICES
from .config import ROOT, settings
from .models import Design, Workload
from .pricing import PriceBook
from .rules import architect
from .terraform import generate

EVAL = ROOT / "eval"
RUNS = EVAL / "runs"
# Infrastructure failures (rate limits, timeouts, 5xx) say nothing about the architect, so --resume re-runs them.
# A model that answers badly is never re-run.
PROVIDER_ERRORS = ("RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError", "RuntimeError: provider")
TERRAFORM = shutil.which("terraform") or str(Path(os.getenv("TERRAFORM", "")) if os.getenv("TERRAFORM") else "")


def load_scenarios() -> list[tuple[str, Workload, dict]]:
    out = []
    for s in yaml.safe_load((EVAL / "scenarios.yaml").read_text(encoding="utf-8")):
        expect = s.pop("expect", {}) or {}
        sid = s.pop("id")
        out.append((sid, Workload(**s), expect))
    return out


def expectations(d: Design, a, expect: dict, w: Workload) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    services = {c.service for c in d.components}
    tiers = {c.tier for c in d.components}
    if "max_total" in expect:
        checks["max_total"] = a.total_nzd_month <= expect["max_total"]
    if "all_in" in expect:
        inside = [c for c in d.components if not SERVICES[c.service].global_service and "llm" not in SERVICES[c.service].roles]
        checks["all_in"] = all(c.region in expect["all_in"] for c in inside)
    if expect.get("private_data"):
        checks["private_data"] = all(
            c.private_endpoint for c in d.components if SERVICES[c.service].data_store and SERVICES[c.service].private_endpoint_ok
        )
    if expect.get("zone_redundant_db"):
        checks["zone_redundant_db"] = any(c.zone_redundant for c in d.components if c.service in ("postgres", "azure_sql", "cosmos_db"))
    for s in expect.get("has", []):
        checks[f"has:{s}"] = s in services
    for t in expect.get("no_tier", []):
        checks[f"no_tier:{t}"] = t not in tiers
    if expect.get("llm_exception"):
        checks["llm_exception"] = any("llm" in e.lower() or "openai" in e.lower() or "res-" in e.lower() for e in d.accepted_exceptions)
    if expect.get("dr_region"):
        checks["dr_region"] = not dr_gaps(d, w)  # a standby and a database copy actually run there
    if expect.get("dr_note"):
        checks["dr_note"] = bool(d.dr_region) or any(
            "region" in t.lower() and ("one" in t.lower() or "single" in t.lower() or "dr" in t.lower()) for t in d.tradeoffs + d.accepted_exceptions
        )
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
        return {"id": sid, "ok": False, "error": extra.get("error", "no design")}
    a = assess(d, w, book)
    checks = expectations(d, a, expect, w)
    row = {
        "id": sid,
        "ok": True,
        "total_nzd": a.total_nzd_month,
        "silent_violations": [v.rule for v in a.violations],
        "within_budget": w.budget_nzd_month is None or a.total_nzd_month <= w.budget_nzd_month,
        "expectations": checks,
        "expectations_met": all(checks.values()) if checks else True,
        "high_findings": sorted({f.rule for f in a.findings if f.severity == "high" and "Accepted:" not in f.message}),
        "exceptions": d.accepted_exceptions,
        "design": d.model_dump(),
        **extra,
    }
    if extra.get("claimed_total_nzd"):
        row["cost_error"] = round(abs(extra["claimed_total_nzd"] - a.total_nzd_month) / max(a.total_nzd_month, 1), 3)
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
    rows = []
    for sid, w, expect in scenarios:
        if keep and sid in keep:
            rows.append(keep[sid])
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
                d, extra = r.design, {"steps": len(r.steps), "claimed_total_nzd": r.claimed_total_nzd, "error": r.error}
        except Exception as exc:  # noqa: BLE001  provider errors are recorded, not fatal
            d, extra = None, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        extra.update({"system": system, "seconds": round(time.time() - t, 1)})
        row = score(sid, w, expect, d, book, extra, check_tf)
        rows.append(row)
        mark = "ok " if row.get("ok") and not row.get("silent_violations") else "!! "
        print(f"  {system:8s} {sid} {mark}{row.get('total_nzd', 0):>9,.0f} NZD  {row.get('silent_violations', row.get('error', ''))}", flush=True)
        if system != "rules" and pause:
            time.sleep(pause)
    return rows


def report(results: dict[str, dict]) -> str:
    names = {"rules": "Rule-based architect (no model)", "agent": "LLM agent with tools", "llm_only": "LLM alone (no tools)"}
    lines = [
        "# Evaluation",
        "",
        "20 workloads (`eval/scenarios.yaml`), every design scored by the same deterministic assessor.",
        "",
        "| System | Model | No silent violations | Within budget | Expectations met | High findings left (avg) | Terraform valid "
        "| LLM's own cost estimate within 20% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for system, res in results.items():
        s = res["summary"]
        pct = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
        lines.append(
            f"| {names.get(system, system)} | {res.get('model') or '-'} | {pct(s['no_silent_violations'])} | {pct(s['within_budget'])} | "
            f"{pct(s['expectations_met'])} | {s['avg_high_findings']} | {pct(s.get('terraform_valid'))} | {pct(s.get('cost_within_20pct'))} |"
        )
    lines += ["", "## Per scenario", "", "| Scenario | " + " | ".join(names.get(s, s) for s in results) + " |", "|---|" + "---|" * len(results)]
    ids = [r["id"] for r in next(iter(results.values()))["rows"]]
    for sid in ids:
        cells = []
        for res in results.values():
            r = next(x for x in res["rows"] if x["id"] == sid)
            if not r.get("ok"):
                cells.append("no design")
                continue
            flags = ("silent: " + ", ".join(r["silent_violations"])) if r["silent_violations"] else "ok"
            missed = [k for k, v in r["expectations"].items() if not v]
            cells.append(f"NZ${r['total_nzd']:,.0f} · {flags}" + (f" · missed {', '.join(missed)}" if missed else ""))
        lines.append(f"| {sid} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    cfg = settings()
    book = PriceBook(cfg.price_cache)
    scenarios = load_scenarios()
    RUNS.mkdir(parents=True, exist_ok=True)
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else ["rules", "agent", "llm_only"]
    check_tf = "--no-terraform" not in argv
    pause = float(os.getenv("EVAL_PAUSE", "0"))
    if "--rescore" not in argv:
        for system in only:
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
    for path in sorted(RUNS.glob("*.json")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        rows = []
        for r in saved["rows"]:
            w, e = by_id[r["id"]]
            d = Design.model_validate(r["design"]) if r.get("design") else None
            keep = {k: r[k] for k in ("system", "seconds", "steps", "claimed_total_nzd", "error") if k in r}
            new = score(r["id"], w, e, d, book, keep, check_tf=False)
            if "terraform_valid" in r:
                new["terraform_valid"] = r["terraform_valid"]
            rows.append(new)
        key = saved["system"] if saved["system"] not in results else f"{saved['system']} ({saved['model']})"
        results[key] = {"model": saved["model"], "rows": rows, "summary": summarise(rows)}
        path.write_text(json.dumps({**saved, "rows": rows}, indent=1), encoding="utf-8")
    order = {"rules": 0, "llm_only": 1, "agent": 2}
    results = dict(sorted(results.items(), key=lambda kv: order.get(kv[0].split(" ")[0], 9)))
    (EVAL / "results.md").write_text(report(results), encoding="utf-8")
    for system, res in results.items():
        print(system, json.dumps(res["summary"]))
    print("wrote eval/results.md")


if __name__ == "__main__":
    main()

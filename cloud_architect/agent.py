"""The architect agent: an LLM that designs with tools, and an LLM-only baseline that designs without them.

The agent drafts a design, calls `assess_design` (live prices, residency and budget checks, the
Well-Architected review), fixes what fails, and calls `submit_design`. A submission with hard
violations is sent back once with the reasons; a constraint that genuinely cannot be met must be
recorded in `accepted_exceptions`, so nothing is relaxed silently.

The baseline gets the same workload, rules and catalogue (with its notes) but no tools: it must
estimate costs from memory. Comparing the two is the point of the evaluation.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from . import llm as llm_mod
from .assess import assess
from .catalogue import options
from .compliance import requirements
from .config import Settings
from .models import Design, Workload
from .pricing import PriceBook
from .regions import ALL, JURISDICTIONS, label, location_region

RULES = """Rules:
1. Respect data residency and company policy: place components only in the allowed regions listed with the
   workload, in the one closest to the users. For disaster recovery use the primary region's pair when it
   is allowed. If a need cannot be met inside the boundary, use the closest compliant option and add an
   entry to accepted_exceptions that starts with the rule code in brackets (e.g. "[RES-REGION] ...") and
   explains what leaves the boundary and why. Never use an exception to avoid a fix that is available.
2. Accepted company policy rules (rule codes POL-*) and stack constraints are hard constraints, like residency.
3. Recovery targets: a 99.99% target or an RTO of 4 hours or less needs a warm standby in a second region;
   an RPO under 60 minutes needs a database replica there; any RPO up to a day needs geo-redundant backups.
4. Use component settings for sizing and compliance: backup_days, geo_backup (1) and ltr_days on databases,
   retention_days (log retention) and audit (1) on log_analytics, max_instances (autoscale) on App Service,
   delete_after_days on storage. Set customer_managed_key and private_endpoint where required, and put any
   required tags in the design's tags.
5. Give every component a one-line purpose. Keep the rationale to 3-5 sentences."""

SYSTEM = (
    """You are a senior Azure solutions architect. Design an architecture for the workload using ONLY services and
tiers from the catalogue (call list_options). Price and check every draft with assess_design: it prices the design
live in the workload's currency and checks residency, budget, company policies and the Well-Architected review.
Never state a price you did not get from it. Fix every hard violation it reports, keep the total within the
budget when there is one, and address high-severity findings unless that breaks the budget (say so in tradeoffs).
When the design is final, call submit_design with it. Do not answer in plain text.
"""
    + RULES
)

COMPONENT_SCHEMA = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "tier": {"type": "string"},
        "region": {"type": "string"},
        "instances": {"type": "integer", "minimum": 1},
        "zone_redundant": {"type": "boolean"},
        "private_endpoint": {"type": "boolean"},
        "customer_managed_key": {"type": "boolean"},
        "purpose": {"type": "string"},
        "settings": {"type": "object", "additionalProperties": {"type": "number"}},
    },
    "required": ["service", "tier", "region"],
}
DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string"},
        "dr_region": {"type": ["string", "null"]},
        "components": {"type": "array", "items": COMPONENT_SCHEMA},
        "rationale": {"type": "string"},
        "tradeoffs": {"type": "array", "items": {"type": "string"}},
        "accepted_exceptions": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["region", "components"],
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_options",
            "description": "The catalogue: services, tiers and notes. Optional role filter.",
            "parameters": {"type": "object", "properties": {"role": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assess_design",
            "description": "Price a design live and check residency, budget, coverage and the Well-Architected review.",
            "parameters": {"type": "object", "properties": {"design": DESIGN_SCHEMA}, "required": ["design"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_design",
            "description": "Submit the final design.",
            "parameters": {"type": "object", "properties": {"design": DESIGN_SCHEMA}, "required": ["design"]},
        },
    },
]


@dataclass
class Run:
    design: Design | None
    steps: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    model: str = ""
    error: str = ""
    claimed_total: float | None = None  # baseline only: what the model said it would cost, in the workload's currency


_client = llm_mod.client
_extra = llm_mod.extra_body
RETRY_WAITS = llm_mod.RETRY_WAITS


def _complete(client, cache=None, min_interval: float = 0.0, **kw):
    """One chat completion, cached, spaced and retried with back-off on rate limits, timeouts and 5xx errors."""
    return llm_mod.complete(client, cache=cache, min_interval=min_interval, retry_waits=RETRY_WAITS, **kw)


def _io(cfg: Settings, injected) -> dict:
    """Cache and spacing apply to a real provider, not to a test double."""
    if injected is not None:
        return {}
    return {"cache": llm_mod.Cache(cfg.llm_cache) if cfg.llm_cache else None, "min_interval": cfg.min_interval}


def _context(w: Workload) -> str:
    allowed = sorted(requirements(w).allowed)
    near = [r for r in (location_region(x) for x in w.user_locations) if r]
    lines = [
        "Workload:",
        w.model_dump_json(indent=1, exclude_defaults=True),
        f"Currency: {w.currency}. Residency: {JURISDICTIONS[w.residency]['label']}.",
        f"Allowed regions for this residency: {allowed}",
        "Region pairs: " + ", ".join(f"{r}->{ALL[r].pair}" for r in allowed if r in ALL and ALL[r].pair),
    ]
    if near:
        lines.append("Users are nearest to: " + ", ".join(f"{r} ({label(r)})" for r in near))
    return "\n".join(lines)


def _summary(a) -> dict:
    return {
        "total_month": a.total_month,
        "currency": a.currency,
        "violations": [f"[{v.rule}] {v.message} Fix: {v.fix}" for v in a.violations],
        "high_findings": [f"[{f.rule}] {f.message} Fix: {f.fix}" for f in a.findings if f.severity == "high" and "Accepted:" not in f.message],
        "other_findings": [f"[{f.rule}] {f.message}" for f in a.findings if f.severity in ("medium", "low")][:8],
        "cost_by_component": _by_component(a),
    }


def _by_component(a) -> dict:
    out: dict[str, float] = {}
    for line in a.lines:
        key = f"{line.service}/{line.tier}@{line.region}"
        out[key] = round(out.get(key, 0) + line.amount, 2)
    return out


def _parse_design(args: str) -> Design:
    data = json.loads(args or "{}")
    return Design.model_validate(data.get("design", data))


def _echo_call(c) -> dict:
    """A tool call as sent back in the history. Provider extras are kept: Gemini needs each call's
    thought signature (``extra_content``) returned with it, or the next request is rejected."""
    extra = getattr(c, "model_extra", None) or {}
    return {**extra, "id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}


def design_with_tools(w: Workload, book: PriceBook, cfg: Settings, client=None, max_steps: int = 12) -> Run:
    io = _io(cfg, client)
    client = client or _client(cfg)
    started = time.time()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _context(w)}]
    run = Run(design=None, model=cfg.llm_model)
    rejected_once = False
    for _ in range(max_steps):
        resp = _complete(
            client, **io, model=cfg.llm_model, messages=messages, tools=TOOLS, temperature=0, max_tokens=cfg.max_tokens, extra_body=_extra(cfg)
        )
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": "Call submit_design with the final design."})
            run.steps.append({"tool": "(text)", "note": (msg.content or "")[:200]})
            continue
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [_echo_call(c) for c in calls],
            }
        )
        for call in calls:
            name, result = call.function.name, {}
            try:
                if name == "list_options":
                    role = json.loads(call.function.arguments or "{}").get("role")
                    result = {"options": options(role)}
                elif name in ("assess_design", "submit_design"):
                    d = _parse_design(call.function.arguments)
                    a = assess(d, w, book)
                    result = _summary(a)
                    if name == "submit_design":
                        if a.violations and not rejected_once:
                            rejected_once = True
                            result = {
                                "rejected": True,
                                **result,
                                "instruction": "Fix the violations or record accepted_exceptions, then submit again.",
                            }
                        else:
                            run.design = d
                else:
                    result = {"error": f"unknown tool {name}"}
            except (ValidationError, json.JSONDecodeError, KeyError) as exc:
                result = {"error": f"invalid design: {str(exc)[:400]}"}
            run.steps.append({"tool": name, "result": {k: v for k, v in result.items() if k != "options"} if isinstance(result, dict) else result})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)[:12000]})
        if run.design is not None:
            break
    run.seconds = round(time.time() - started, 1)
    if run.design is None:
        run.error = "the model did not submit a design within the step limit"
    return run


BASELINE = (
    """You are a senior Azure solutions architect. Design an architecture for the workload using ONLY services and
tiers from the catalogue below. You have no tools: estimate the monthly cost in the workload's currency from your own
knowledge. Keep the total within the budget when there is one, and follow Azure Well-Architected practice for
reliability, security, cost and operations.
"""
    + RULES
    + """
Reply with JSON only: {"design": {"region", "dr_region", "components": [{"service", "tier", "region", "instances",
"zone_redundant", "private_endpoint", "customer_managed_key", "purpose", "settings"}], "rationale", "tradeoffs",
"accepted_exceptions", "tags"}, "estimated_total_month": number}
Catalogue:
"""
)


def _parse_reply(raw: str) -> tuple[Design, float | None]:
    data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    claimed = data.get("estimated_total_month", data.get("estimated_total_nzd_month"))
    return Design.model_validate(data["design"]), float(claimed or 0) or None


def design_without_tools(w: Workload, cfg: Settings, client=None) -> Run:
    """The baseline. A reply that does not parse gets one repair turn, as the agent gets one rejected submission."""
    io = _io(cfg, client)
    client = client or _client(cfg)
    started = time.time()
    messages = [{"role": "system", "content": BASELINE + json.dumps(options())}, {"role": "user", "content": _context(w)}]
    run = Run(design=None, model=cfg.llm_model)
    for attempt in range(2):
        resp = _complete(client, **io, model=cfg.llm_model, temperature=0, max_tokens=cfg.max_tokens, extra_body=_extra(cfg), messages=messages)
        choice = resp.choices[0]
        raw = choice.message.content or ""
        try:
            run.design, run.claimed_total = _parse_reply(raw)
            run.error = ""
            break
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            run.error = f"unparseable reply ({len(raw)} chars, finish_reason={getattr(choice, 'finish_reason', '?')}): {str(exc)[:160]}"
            run.steps.append({"tool": "(repair)" if attempt else "(reply)", "note": run.error})
            messages += [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": f"That reply could not be parsed ({str(exc)[:160]}). Reply again with the JSON object only."},
            ]
    run.seconds = round(time.time() - started, 1)
    return run

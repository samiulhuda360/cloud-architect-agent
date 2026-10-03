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

from .assess import assess
from .catalogue import options
from .config import Settings
from .models import REGIONS, RESIDENCY_REGIONS, Design, Workload
from .pricing import PriceBook

REGION_LIST = ", ".join(f"{k} ({v})" for k, v in REGIONS.items())

SYSTEM = (
    """You are a senior Azure solutions architect for New Zealand organisations. Design an architecture for
the workload using ONLY services and tiers from the catalogue (call list_options). Rules:
1. Price and check every draft with assess_design; never state a price you did not get from it.
2. Fix every hard violation it reports. Keep the total within the budget when there is one.
3. Respect data residency: 'nz' means New Zealand North only, 'anz' adds Australia East and Southeast.
   If a need cannot be met inside the boundary, use the closest compliant option and add an entry to
   accepted_exceptions that starts with the rule code in brackets (e.g. "[RES-REGION] ...") and explains
   what leaves the boundary and why. Never use an exception to avoid a fix that is available.
4. Address high-severity review findings unless doing so breaks the budget; say so in tradeoffs if not.
5. Give every component a one-line purpose. Keep the rationale to 3-5 sentences.
6. When the design is final, call submit_design with it. Do not answer in plain text.
Regions: """
    + REGION_LIST
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
    claimed_total_nzd: float | None = None  # baseline only: what the model said it would cost


def _client(cfg: Settings):
    import openai

    return openai.OpenAI(api_key=cfg.llm_api_key or "not-needed", base_url=cfg.llm_base_url, timeout=180)


def _extra(cfg: Settings) -> dict | None:
    return {"reasoning": {"effort": "low"}} if "openrouter.ai" in cfg.llm_base_url else None


RETRY_WAITS = (15, 30, 60, 120)  # seconds; free and shared endpoints rate-limit in bursts


def _complete(client, **kw):
    """One chat completion, retried with back-off on rate limits, timeouts and 5xx errors."""
    import openai

    for wait in (*RETRY_WAITS, None):
        try:
            resp = client.chat.completions.create(**kw)
            if resp.choices:
                return resp
            err: Exception = RuntimeError("provider returned no choices")
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
            if "per-day" in str(exc) or "per day" in str(exc):  # a daily quota will not clear in minutes
                raise
            err = exc
        if wait is None:
            raise err
        time.sleep(wait)


def _context(w: Workload) -> str:
    return "Workload:\n" + w.model_dump_json(indent=1) + f"\nAllowed regions for this residency: {sorted(RESIDENCY_REGIONS[w.residency])}"


def _summary(a) -> dict:
    return {
        "total_nzd_month": a.total_nzd_month,
        "violations": [f"[{v.rule}] {v.message} Fix: {v.fix}" for v in a.violations],
        "high_findings": [f"[{f.rule}] {f.message} Fix: {f.fix}" for f in a.findings if f.severity == "high"],
        "other_findings": [f"[{f.rule}] {f.message}" for f in a.findings if f.severity in ("medium", "low")][:8],
        "cost_by_component": _by_component(a),
    }


def _by_component(a) -> dict:
    out: dict[str, float] = {}
    for line in a.lines:
        key = f"{line.service}/{line.tier}@{line.region}"
        out[key] = round(out.get(key, 0) + line.nzd_month, 2)
    return out


def _parse_design(args: str) -> Design:
    data = json.loads(args or "{}")
    return Design.model_validate(data.get("design", data))


def design_with_tools(w: Workload, book: PriceBook, cfg: Settings, client=None, max_steps: int = 12) -> Run:
    client = client or _client(cfg)
    started = time.time()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _context(w)}]
    run = Run(design=None, model=cfg.llm_model)
    rejected_once = False
    for _ in range(max_steps):
        resp = _complete(
            client, model=cfg.llm_model, messages=messages, tools=TOOLS, temperature=0, max_tokens=cfg.max_tokens, extra_body=_extra(cfg)
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
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls
                ],
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
    """You are a senior Azure solutions architect for New Zealand organisations. Design an architecture for the
workload using ONLY services and tiers from the catalogue below. You have no tools: estimate the monthly cost in NZD
from your own knowledge. Rules:
1. Keep the total within the budget when there is one.
2. Respect data residency: 'nz' means New Zealand North only, 'anz' adds Australia East and Southeast.
   If a need cannot be met inside the boundary, use the closest compliant option and add an entry to
   accepted_exceptions that starts with the rule code in brackets (e.g. "[RES-REGION] ...") and explains
   what leaves the boundary and why. Never use an exception to avoid a fix that is available.
3. Follow Azure Well-Architected practice for reliability, security, cost and operations.
4. Give every component a one-line purpose. Keep the rationale to 3-5 sentences.
Reply with JSON only: {"design": {"region", "dr_region", "components": [{"service", "tier", "region", "instances",
"zone_redundant", "private_endpoint", "purpose"}], "rationale", "tradeoffs", "accepted_exceptions"}, "estimated_total_nzd_month": number}
Regions: """
    + REGION_LIST
    + """
Catalogue:
"""
)


def _parse_reply(raw: str) -> tuple[Design, float | None]:
    data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    return Design.model_validate(data["design"]), float(data.get("estimated_total_nzd_month") or 0) or None


def design_without_tools(w: Workload, cfg: Settings, client=None) -> Run:
    """The baseline. A reply that does not parse gets one repair turn, as the agent gets one rejected submission."""
    client = client or _client(cfg)
    started = time.time()
    messages = [{"role": "system", "content": BASELINE + json.dumps(options())}, {"role": "user", "content": _context(w)}]
    run = Run(design=None, model=cfg.llm_model)
    for attempt in range(2):
        resp = _complete(client, model=cfg.llm_model, temperature=0, max_tokens=cfg.max_tokens, extra_body=_extra(cfg), messages=messages)
        choice = resp.choices[0]
        raw = choice.message.content or ""
        try:
            run.design, run.claimed_total_nzd = _parse_reply(raw)
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

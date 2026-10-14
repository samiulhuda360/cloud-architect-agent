"""Web API and UI.

GET  /                      the web UI
GET  /api/presets           the real-life examples and the evaluation scenarios, as full workloads
GET  /api/options           the service catalogue, regions, jurisdictions and currencies
POST /api/design            {"workload": {...}, "mode": "agent" | "rules"} -> design, price, review, diagrams, Terraform, report
POST /api/assess            {"workload": {...}, "design": {...}} -> the same for an edited design
POST /api/policies/extract  {"filename": "...", "content_b64": "..."} -> proposed policy rules to review
POST /api/brief/extract     {"filename": "...", "content_b64": "..."} -> proposed form values to confirm
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import brief, catalogue, policies
from .agent import design_with_tools
from .assess import assess
from .config import ROOT, settings
from .diagram import drawio, mermaid, svg
from .documents import SUPPORTED
from .evaluate import load_examples, load_scenarios
from .models import Design, Workload
from .pricing import PriceBook
from .regions import ALL, CURRENCIES, JURISDICTIONS, RESIDENCY_REGIONS
from .report import report
from .rules import architect
from .terraform import generate

app = FastAPI(
    title="Cloud Architect Agent",
    version="2.0.0",
    description="Azure architectures for any region: live prices in your currency, data residency, company policies, "
    "Well-Architected review, diagrams and Terraform.",
)
MAX_UPLOAD = 5 * 1024 * 1024


@lru_cache(maxsize=1)
def book() -> PriceBook:
    return PriceBook(settings().price_cache)


class DesignIn(BaseModel):
    workload: Workload
    mode: Literal["agent", "rules"] = "agent"


class AssessIn(BaseModel):
    workload: Workload
    design: Design


class Upload(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_b64: str


def _result(d: Design, w: Workload, **extra) -> dict:
    a = assess(d, w, book())
    return {
        "design": d.model_dump(),
        "total_month": a.total_month,
        "currency": a.currency,
        "budget_month": w.budget_month,
        "by_environment": a.by_environment,
        "sizing": a.sizing,
        "lines": [line.model_dump() for line in a.lines],
        "violations": [v.model_dump() for v in a.violations],
        "findings": [f.model_dump() for f in a.findings],
        "fx_rate": a.fx_rate,
        "diagram": mermaid(d, w),
        "drawio": drawio(d, w),
        "svg": svg(d, w),
        "terraform": generate(d, w),
        "report": report(d, w, a),
        "policies": [p.model_dump() for p in w.accepted_policies],
        **extra,
    }


def _decode(body: Upload) -> bytes:
    if not body.filename.lower().endswith(SUPPORTED):
        raise HTTPException(415, "Upload a PDF, Word (.docx), Markdown or text file.")
    try:
        data = base64.b64decode(body.content_b64, validate=True)
    except ValueError as exc:
        raise HTTPException(422, "The file content is not valid base64.") from exc
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "The file is larger than 5 MB.")
    return data


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(ROOT / "ui" / "index.html")


@app.get("/api/presets")
def presets():
    # Full workloads, defaults included, so the form shows exactly what is designed and scored.
    examples = [{"id": sid, "group": "Real-life examples", **w.model_dump()} for sid, w in load_examples()]
    scenarios = [{"id": sid, "group": "Evaluation scenarios", **w.model_dump()} for sid, w, _ in load_scenarios()]
    return examples + scenarios


@app.get("/api/options")
def options():
    return {
        "services": catalogue.options(),
        "jurisdictions": {
            k: {"label": v["label"], "currency": v["currency"], "regions": sorted(RESIDENCY_REGIONS[k])} for k, v in JURISDICTIONS.items()
        },
        "regions": {r.code: {"label": r.label, "pair": r.pair, "restricted": r.restricted} for r in ALL.values()},
        "currencies": list(CURRENCIES),
        "policy_checks": policies.CHECKS,
    }


@app.post("/api/design")
def design(body: DesignIn):
    cfg = settings()
    w = body.workload
    if body.mode == "agent" and cfg.llm_configured:
        try:
            run = design_with_tools(w, book(), cfg)
        except Exception as exc:  # noqa: BLE001  provider down or out of credit: fall back, and say so
            d = architect(w, book())
            return _result(d, w, mode="rules", note=f"The AI agent was unavailable ({str(exc)[:120]}); this is the rule-based design.")
        if run.design is None:
            d = architect(w, book())
            return _result(d, w, mode="rules", note=f"The AI agent did not finish ({run.error}); this is the rule-based design.")
        return _result(run.design, w, mode="agent", model=run.model, seconds=run.seconds, steps=len(run.steps))
    d = architect(w, book())
    note = "" if body.mode == "rules" else "No language model is configured; this is the rule-based design."
    return _result(d, w, mode="rules", note=note)


@app.post("/api/assess")
def reassess(body: AssessIn):
    try:
        return _result(body.design, body.workload, mode="edited")
    except KeyError as exc:
        raise HTTPException(422, f"Unknown service or tier: {exc}") from exc


@app.post("/api/policies/extract")
def extract_policies(body: Upload):
    data = _decode(body)
    try:
        return policies.extract(body.filename, data, settings())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/brief/extract")
def extract_brief(body: Upload):
    data = _decode(body)
    try:
        return brief.extract(body.filename, data, settings())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

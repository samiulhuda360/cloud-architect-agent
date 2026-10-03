"""Web API and UI.

GET  /                 the web UI
GET  /api/presets      example workloads (the evaluation scenarios)
GET  /api/options      the service catalogue
POST /api/design       {"workload": {...}, "mode": "agent" | "rules"} -> design, price, review, diagram, Terraform
POST /api/assess       {"workload": {...}, "design": {...}} -> price and review for an edited design
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import catalogue
from .agent import design_with_tools
from .assess import assess
from .config import ROOT, settings
from .diagram import mermaid
from .evaluate import load_scenarios
from .models import Design, Workload
from .pricing import PriceBook
from .rules import architect
from .terraform import generate

app = FastAPI(
    title="Cloud Architect Agent",
    version="1.0.0",
    description="Azure architectures for New Zealand workloads: live NZD pricing, data residency, Well-Architected review, Terraform.",
)


@lru_cache(maxsize=1)
def book() -> PriceBook:
    return PriceBook(settings().price_cache)


class DesignIn(BaseModel):
    workload: Workload
    mode: Literal["agent", "rules"] = "agent"


class AssessIn(BaseModel):
    workload: Workload
    design: Design


def _result(d: Design, w: Workload, **extra) -> dict:
    a = assess(d, w, book())
    return {
        "design": d.model_dump(),
        "total_nzd_month": a.total_nzd_month,
        "budget_nzd_month": w.budget_nzd_month,
        "lines": [line.model_dump() for line in a.lines],
        "violations": [v.model_dump() for v in a.violations],
        "findings": [f.model_dump() for f in a.findings],
        "fx_usd_to_nzd": a.fx_usd_to_nzd,
        "diagram": mermaid(d, w),
        "terraform": generate(d, w),
        **extra,
    }


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(ROOT / "ui" / "index.html")


@app.get("/api/presets")
def presets():
    # Full workloads, defaults included, so the form shows exactly what the evaluation scores.
    return [{"id": sid, **w.model_dump()} for sid, w, _ in load_scenarios()]


@app.get("/api/options")
def options():
    return catalogue.options()


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

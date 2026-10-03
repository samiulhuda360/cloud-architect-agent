"""Command line: `python -m cloud_architect <command>`.

design   design a workload from a YAML/JSON file (agent if an LLM is configured, else rules)
eval     evaluate the architects on eval/scenarios.yaml (see evaluate.py for flags)
serve    web UI and API on http://127.0.0.1:8000
mcp      MCP server (stdio) exposing pricing, residency and review tools
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .config import settings


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="cloud_architect")
    sub = p.add_subparsers(dest="cmd", required=True)
    pd = sub.add_parser("design")
    pd.add_argument("workload", help="YAML or JSON file describing the workload")
    pd.add_argument("--rules", action="store_true", help="use the rule-based architect, no model")
    pd.add_argument("--terraform", help="write the Terraform to this folder")
    sub.add_parser("eval", help="flags: --only rules,agent,llm_only  --rescore  --no-terraform")
    ps = sub.add_parser("serve")
    ps.add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp")
    a, rest = p.parse_known_args(argv)
    if rest and a.cmd != "eval":
        p.error(f"unrecognized arguments: {' '.join(rest)}")

    if a.cmd == "design":
        from .agent import design_with_tools
        from .assess import assess
        from .models import Workload
        from .pricing import PriceBook
        from .rules import architect
        from .terraform import generate

        cfg = settings()
        book = PriceBook(cfg.price_cache)
        w = Workload(**yaml.safe_load(Path(a.workload).read_text(encoding="utf-8")))
        d = architect(w, book) if a.rules or not cfg.llm_configured else design_with_tools(w, book, cfg).design
        if d is None:
            print("The agent did not produce a design; falling back to the rule-based architect.")
            d = architect(w, book)
        res = assess(d, w, book)
        print(
            json.dumps(
                {
                    "design": d.model_dump(),
                    "total_nzd_month": res.total_nzd_month,
                    "violations": [v.model_dump() for v in res.violations],
                    "findings": [f.model_dump() for f in res.findings],
                },
                indent=1,
            )
        )
        if a.terraform:
            out = Path(a.terraform)
            out.mkdir(parents=True, exist_ok=True)
            (out / "main.tf").write_text(generate(d, w), encoding="utf-8")
            print(f"Terraform written to {out / 'main.tf'}")
    elif a.cmd == "eval":
        from .evaluate import main as evaluate

        evaluate(rest)
    elif a.cmd == "serve":
        import uvicorn

        uvicorn.run("cloud_architect.api:app", host="127.0.0.1", port=a.port)
    elif a.cmd == "mcp":
        from .mcp_server import main as mcp

        mcp()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

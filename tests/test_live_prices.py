"""Live check against the Azure Retail Prices API: every catalogue recipe still matches a real meter.

Skipped unless LIVE_PRICES=1. CI runs it weekly, so a renamed Azure meter fails the build instead of
pricing a component at zero.
"""

import os

import pytest

from cloud_architect.assess import assess
from cloud_architect.catalogue import SERVICES, ambiguous_recipes
from cloud_architect.models import Component, Design, Workload
from cloud_architect.pricing import PriceBook
from cloud_architect.regions import sold

pytestmark = pytest.mark.skipif(os.getenv("LIVE_PRICES") != "1", reason="set LIVE_PRICES=1 to query the live Azure API")


def test_every_recipe_matches_a_live_meter(tmp_path):
    book = PriceBook(tmp_path / "prices.sqlite")
    w = Workload(name="live check", needs=["web_app"], peak_rps=20, blob_gb=100, llm_input_mtokens=10, llm_output_mtokens=2, monthly_users=100_000)
    missing = []
    for sid, svc in SERVICES.items():
        for tier in svc.tiers:
            region = next(r for r in ("newzealandnorth", "australiaeast", "uksouth", "eastus2") if sold(sid, tier, r))
            d = Design(region=region, components=[Component(service=sid, tier=tier, region=region)])
            a = assess(d, w, book)
            if any(v.rule == "NOT-SOLD" for v in a.violations) or a.total_month <= 0:
                missing.append(f"{sid}/{tier} in {region}")
    assert not missing, f"recipes that no longer match a live meter: {missing}"


def test_no_recipe_matches_two_prices(tmp_path):
    assert not ambiguous_recipes(PriceBook(tmp_path / "prices.sqlite"))

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

pytestmark = pytest.mark.skipif(os.getenv("LIVE_PRICES") != "1", reason="set LIVE_PRICES=1 to query the live Azure API")


def test_every_recipe_matches_a_live_meter(tmp_path):
    book = PriceBook(tmp_path / "prices.sqlite")
    w = Workload(name="live check", needs=["web_app"], peak_rps=20, blob_gb=100, llm_input_mtokens=10, llm_output_mtokens=2)
    missing = []
    for sid, svc in SERVICES.items():
        for tier in svc.tiers:
            region = "australiaeast" if sid == "azure_openai" or (sid == "blob_storage" and tier == "hot_grs") else "newzealandnorth"
            d = Design(region=region, components=[Component(service=sid, tier=tier, region=region)])
            a = assess(d, w, book)
            if any(v.rule == "NOT-SOLD" for v in a.violations) or a.total_nzd_month <= 0:
                missing.append(f"{sid}/{tier} in {region}")
    assert not missing, f"recipes that no longer match a live meter: {missing}"


def test_no_recipe_matches_two_prices(tmp_path):
    assert not ambiguous_recipes(PriceBook(tmp_path / "prices.sqlite"))

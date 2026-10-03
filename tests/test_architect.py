import json
import os
import re

import pytest

from cloud_architect.agent import design_with_tools, design_without_tools
from cloud_architect.assess import assess
from cloud_architect.catalogue import SERVICES, ambiguous_recipes
from cloud_architect.config import Settings
from cloud_architect.evaluate import load_scenarios
from cloud_architect.models import Component, Design, Workload
from cloud_architect.pricing import Meter, tiered_cost
from cloud_architect.rules import architect
from cloud_architect.terraform import generate

CFG = Settings(llm_api_key="test")


def comp(service, tier, region="newzealandnorth", **kw):
    return Component(service=service, tier=tier, region=region, **kw)


def web(**kw):
    base = {"name": "Test", "needs": ["web_app", "relational_db"], "budget_nzd_month": 5000}
    return Workload(**{**base, **kw})


# ---------------------------------------------------------------- pricing
def test_tiered_cost_applies_each_band():
    rows = [Meter("p", "s", "m", "1 GB", 0.0, 0, "r"), Meter("p", "s", "m", "1 GB", 2.0, 5, "r")]
    assert tiered_cost(rows, 3) == 0 and tiered_cost(rows, 8) == pytest.approx(6.0)


def test_fx_is_derived_from_azures_own_prices(book):
    assert 1.4 < book.usd_to_nzd() < 2.2


def test_every_catalogue_tier_prices_in_nz_north_or_australia(book):
    # peak_rps 20 pushes log ingestion past Log Analytics' 5 GB monthly free allowance
    w = web(blob_gb=100, llm_input_mtokens=10, llm_output_mtokens=2, peak_rps=20)
    for sid, svc in SERVICES.items():
        for tier in svc.tiers:
            region = "australiaeast" if sid == "azure_openai" else "newzealandnorth"
            if sid == "blob_storage" and tier == "hot_grs":
                region = "australiaeast"  # NZ North has no paired region, so no GRS
            a = assess(Design(region=region, components=[comp(sid, tier, region)]), w, book)
            assert not [v for v in a.violations if v.rule == "NOT-SOLD"], f"{sid}/{tier}"
            assert a.total_nzd_month > 0, f"{sid}/{tier} priced at zero"


def test_every_recipe_matches_one_price_per_band(book):
    assert not ambiguous_recipes(book)


def test_free_allowances_are_applied(book):
    small = assess(Design(region="newzealandnorth", components=[comp("log_analytics", "pay_as_you_go")]), web(peak_rps=1), book)
    assert small.total_nzd_month == 0  # under 5 GB a month


# ---------------------------------------------------------------- constraints
def test_region_outside_residency_is_a_violation(book):
    d = Design(region="australiaeast", components=[comp("app_service", "S1", "australiaeast"), comp("postgres", "B2s", "australiaeast")])
    rules = {v.rule for v in assess(d, web(residency="nz"), book).violations}
    assert "RES-REGION" in rules


def test_exception_naming_the_rule_is_accepted_and_reported(book):
    d = Design(
        region="newzealandnorth",
        components=[comp("app_service", "S1"), comp("postgres", "B2s"), comp("azure_openai", "o4-mini_datazone", "australiaeast")],
        accepted_exceptions=["[RES-REGION] [RES-AI-ZONE] no Azure model is sold in NZ North"],
    )
    a = assess(d, web(needs=["web_app", "relational_db", "llm"], residency="nz"), book)
    assert not a.violations
    assert any("Accepted:" in f.message for f in a.findings)


def test_global_ai_deployment_with_personal_data_is_a_violation(book):
    d = Design(
        region="newzealandnorth",
        components=[comp("app_service", "S1"), comp("postgres", "B2s"), comp("azure_openai", "gpt-4o-mini_global", "australiaeast")],
    )
    rules = {v.rule for v in assess(d, web(needs=["web_app", "relational_db", "llm"], pii=True), book).violations}
    assert "RES-AI-GLOBAL" in rules


def test_budget_and_coverage_violations(book):
    d = Design(region="newzealandnorth", components=[comp("app_service", "P2v3", instances=3)])
    rules = {v.rule for v in assess(d, web(budget_nzd_month=100), book).violations}
    assert {"BUDGET", "NEED"} <= rules


def test_service_not_sold_in_region(book):
    d = Design(region="newzealandnorth", components=[comp("app_service", "S1"), comp("postgres", "B2s"), comp("blob_storage", "hot_grs")])
    assert "NOT-SOLD" in {v.rule for v in assess(d, web(), book).violations}


def test_review_flags_single_zone_and_public_personal_data(book):
    d = Design(region="newzealandnorth", components=[comp("app_service", "S1"), comp("postgres", "D2ds_v5")])
    rules = {f.rule for f in assess(d, web(availability="high", pii=True), book).findings}
    assert {"REL-ZONES", "REL-DB-HA", "SEC-PRIVATE", "SEC-SECRETS", "OPS-LOGS"} <= rules


# ---------------------------------------------------------------- rule-based architect
@pytest.mark.parametrize("sid,w,expect", load_scenarios(), ids=[s[0] for s in load_scenarios()])
def test_rules_architect_never_breaks_residency_silently(book, sid, w, expect):
    a = assess(architect(w, book), w, book)
    assert not [v for v in a.violations if v.rule.startswith("RES")]
    assert not [v for v in a.violations if v.rule == "NEED"]


def test_rules_architect_downsizes_to_fit_a_budget(book):
    w = web(availability="high", budget_nzd_month=1200)
    d = architect(w, book)
    assert assess(d, w, book).total_nzd_month <= 1200 and any("to fit the budget" in t for t in d.tradeoffs)


# ---------------------------------------------------------------- terraform
def test_terraform_has_every_component_and_balanced_braces(book):
    w = web(needs=["web_app", "relational_db", "object_storage"], availability="high", pii=True)
    d = architect(w, book)
    tf = generate(d, w)
    assert tf.count("{") == tf.count("}")
    assert "zone_balancing_enabled = true" in tf and 'mode                      = "ZoneRedundant"' in tf
    assert "azurerm_private_endpoint" in tf and "public_network_access_enabled = false" in tf
    assert not re.search(r"password\s*=\s*\"", tf), "no literal secrets"


@pytest.mark.parametrize("sid,w,expect", load_scenarios(), ids=[s[0] for s in load_scenarios()])
def test_terraform_only_references_resources_it_declares(book, sid, w, expect):
    tf = generate(architect(w, book), w)
    declared = {f"{t}.{n}" for t, n in re.findall(r'^resource "(\w+)" "(\w+)"', tf, re.M)}
    declared |= {f"data.{t}.{n}" for t, n in re.findall(r'^data "(\w+)" "(\w+)"', tf, re.M)}
    refs = {f"{d}{t}.{n}" for d, t, n in re.findall(r"(data\.)?\b((?:azurerm|random)_\w+)\.(\w+)", tf)}
    assert refs <= declared, f"undeclared: {sorted(refs - declared)}"
    assert "access_key" not in tf, "use managed identities, not storage keys"


@pytest.mark.skipif(not os.getenv("TERRAFORM"), reason="set TERRAFORM=path/to/terraform to run")
def test_terraform_validates(book, tmp_path):
    from cloud_architect.evaluate import terraform_valid

    w = web(needs=["web_app", "relational_db", "llm"], residency="anz", availability="critical", pii=True)
    assert terraform_valid(architect(w, book), w, tmp_path)


# ---------------------------------------------------------------- agent
def test_agent_prices_fixes_and_submits(book, fake_llm):
    w = web(residency="nz")
    bad = {
        "region": "australiaeast",
        "components": [
            {"service": "app_service", "tier": "S1", "region": "australiaeast"},
            {"service": "postgres", "tier": "B2s", "region": "australiaeast"},
        ],
    }
    good = {
        "region": "newzealandnorth",
        "components": [
            {"service": "app_service", "tier": "S1", "region": "newzealandnorth"},
            {"service": "postgres", "tier": "B2s", "region": "newzealandnorth"},
            {"service": "key_vault", "tier": "standard", "region": "newzealandnorth"},
        ],
    }
    llm = fake_llm([[("list_options", {})], [("assess_design", {"design": bad})], [("submit_design", {"design": good})]])
    run = design_with_tools(w, book, CFG, client=llm)
    assert run.design and run.design.region == "newzealandnorth"
    assessed = [s for s in run.steps if s["tool"] == "assess_design"][0]["result"]
    assert any("RES-REGION" in v for v in assessed["violations"])


def test_agent_submission_with_violations_is_sent_back_once(book, fake_llm):
    w = web(residency="nz")
    bad = {
        "region": "australiaeast",
        "components": [
            {"service": "app_service", "tier": "S1", "region": "australiaeast"},
            {"service": "postgres", "tier": "B2s", "region": "australiaeast"},
        ],
    }
    llm = fake_llm([[("submit_design", {"design": bad})], [("submit_design", {"design": bad})]])
    run = design_with_tools(w, book, CFG, client=llm)
    assert run.steps[0]["result"]["rejected"] is True and run.design is not None


def test_baseline_parses_design_and_claimed_cost(fake_llm):
    design = {"region": "newzealandnorth", "components": [{"service": "app_service", "tier": "S1", "region": "newzealandnorth"}]}
    reply = "Here it is: " + json.dumps({"design": design, "estimated_total_nzd_month": 250})
    run = design_without_tools(web(), CFG, client=fake_llm([reply]))
    assert run.design and run.claimed_total_nzd == 250


def test_baseline_gets_the_same_knowledge_minus_the_tools(fake_llm):
    design = {"region": "newzealandnorth", "components": [{"service": "app_service", "tier": "S1", "region": "newzealandnorth"}]}
    llm = fake_llm([json.dumps({"design": design, "estimated_total_nzd_month": 250})])
    design_without_tools(web(residency="nz"), CFG, client=llm)
    system, user = llm.seen[0]["messages"][0]["content"], llm.seen[0]["messages"][1]["content"]
    assert "newzealandnorth" in system and "residency_note" in system and "[RES-REGION]" in system
    assert "Allowed regions for this residency: ['newzealandnorth']" in user and "tools" not in llm.seen[0]


def test_baseline_gets_one_repair_turn(fake_llm):
    design = {"region": "newzealandnorth", "components": [{"service": "app_service", "tier": "S1", "region": "newzealandnorth"}]}
    run = design_without_tools(web(), CFG, client=fake_llm(["", json.dumps({"design": design})]))
    assert run.design and not run.error and run.steps[0]["tool"] == "(reply)"


def test_names_are_normalised_before_judging(book):
    d = Design.model_validate(
        {
            "region": "New Zealand North",
            "components": [{"service": "App Service", "tier": "p1 v3", "region": "new zealand north"}, comp("postgres", "b2s").model_dump()],
        }
    )
    assert d.region == "newzealandnorth" and (d.components[0].service, d.components[0].tier) == ("app_service", "P1v3")
    assert d.components[1].tier == "B2s"
    assert not [v for v in assess(d, web(), book).violations if v.rule in ("UNKNOWN", "REGION")]


def test_provider_rate_limits_are_retried(monkeypatch, fake_llm):
    import httpx
    import openai

    from cloud_architect import agent

    monkeypatch.setattr(agent, "RETRY_WAITS", (0, 0))
    llm = fake_llm(["ok"])
    real, calls = llm.chat.completions.create, []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise openai.RateLimitError("429", response=httpx.Response(429, request=httpx.Request("POST", "http://x")), body=None)
        return real(**kw)

    llm.chat.completions.create = flaky
    assert agent._complete(llm, model="m", messages=[{"role": "user", "content": "hi"}]).choices[0].message.content == "ok"
    assert len(calls) == 2


def test_zone_redundant_functions_pay_for_two_always_ready_instances(book):
    w = web(needs=["background_jobs"], availability="high")
    plain = assess(Design(region="newzealandnorth", components=[comp("functions", "flex")]), w, book)
    zr = assess(Design(region="newzealandnorth", components=[comp("functions", "flex", zone_redundant=True)]), w, book)
    baseline = next(line for line in zr.lines if line.item == "always-ready baseline")
    assert baseline.quantity == 2 * 2.0 * 730 * 3600 and baseline.nzd_month > 80 and zr.total_nzd_month > plain.total_nzd_month
    assert "REL-ZONES" in {f.rule for f in plain.findings} and "REL-ZONES" not in {f.rule for f in zr.findings}


def test_zone_redundant_app_service_bills_azures_two_instance_minimum(book):
    one = assess(Design(region="newzealandnorth", components=[comp("app_service", "P0v3", zone_redundant=True)]), web(needs=["web_app"]), book)
    assert one.lines[0].quantity == 2 * 730


def test_free_grant_is_not_counted_twice_where_azure_lists_it_as_a_band(book):
    # Australia East publishes the Flex Consumption free grant as a $0 first band; NZ North does not.
    w = web(needs=["background_jobs"])
    au = assess(Design(region="australiaeast", components=[comp("functions", "flex", "australiaeast")]), w, book)
    nz = assess(Design(region="newzealandnorth", components=[comp("functions", "flex")]), w, book)
    gb_s = next(line for line in au.lines if line.item == "execution time")
    assert gb_s.nzd_month == pytest.approx((1_000_000 - 100_000) * 0.000037 * book.usd_to_nzd(), rel=0.01)
    assert au.total_nzd_month < nz.total_nzd_month


# ---------------------------------------------------------------- disaster recovery
def bank():
    return web(needs=["web_app", "relational_db"], residency="anz", availability="critical", pii=True, budget_nzd_month=15000)


def test_naming_a_dr_region_is_not_dr(book):
    d = Design(
        region="newzealandnorth",
        dr_region="australiaeast",
        components=[comp("app_service", "P1v3", instances=3, zone_redundant=True), comp("postgres", "D2ds_v5", zone_redundant=True)],
    )
    dr = [f for f in assess(d, bank(), book).findings if f.rule == "REL-DR"]
    assert dr and "nothing can take over" in dr[0].message


def test_rules_architect_builds_a_working_dr_site(book):
    w = bank()
    d = architect(w, book)
    a = assess(d, w, book)
    in_dr = {(c.service, bool(c.settings.get("replica"))) for c in d.components if c.region == "australiaeast"}
    assert {("app_service", False), ("postgres", True)} <= in_dr
    assert any(c.service == "front_door" and c.tier == "premium" for c in d.components)
    assert not {"REL-DR", "REL-FAILOVER", "SEC-WAF"} & {f.rule for f in a.findings} and not a.violations
    tf = generate(d, w)
    assert re.search(r'create_mode\s+= "Replica"', tf) and tf.count('resource "azurerm_cdn_frontdoor_origin"') == 2
    assert re.search(r"priority\s+= 1", tf) and re.search(r"priority\s+= 2", tf)  # primary serves, DR takes over
    assert "azurerm_cdn_frontdoor_firewall_policy" in tf and "private_dns_zone_group" in tf and "virtual_network_subnet_id" in tf


@pytest.mark.skipif(not os.getenv("TERRAFORM"), reason="set TERRAFORM=path/to/terraform to run")
@pytest.mark.parametrize("sid,w,expect", load_scenarios(), ids=[s[0] for s in load_scenarios()])
def test_terraform_is_fmt_clean(book, sid, w, expect):
    import subprocess

    tf = generate(architect(w, book), w)
    out = subprocess.run([os.environ["TERRAFORM"], "fmt", "-"], input=tf, capture_output=True, text=True, check=True).stdout
    assert out == tf


def test_functions_reach_storage_with_their_identity(book):
    tf = generate(Design(region="newzealandnorth", components=[comp("functions", "flex", zone_redundant=True)]), web(needs=["background_jobs"]))
    assert "SystemAssignedIdentity" in tf and tf.count("azurerm_role_assignment") == 2
    assert "access_key" not in tf and "connection_string" not in tf.lower()

"""Global regions and currencies, and every workload input changing the design or the checks."""

import pytest
import yaml

from cloud_architect.assess import assess
from cloud_architect.compliance import stack_rules
from cloud_architect.models import Component, Design, Workload
from cloud_architect.regions import RESIDENCY_REGIONS, location_region, primary_region
from cloud_architect.rules import architect
from cloud_architect.terraform import generate


def wl(**kw):
    base = {"name": "Test", "needs": ["web_app", "relational_db"]}
    return Workload(**{**base, **kw})


def rules_of(a):
    return {v.rule for v in a.violations} | {f.rule for f in a.findings}


def comp(service, tier, region="uksouth", **kw):
    return Component(service=service, tier=tier, region=region, **kw)


# ---------------------------------------------------------------- regions, residency and currency
def test_older_workload_files_still_load():
    w = Workload(**yaml.safe_load("name: Old\nneeds: [web_app]\nresidency: nz\nbudget_nzd_month: 900\nenvironment: dev\nuptime_target: '99.95'"))
    assert (w.budget_month, w.currency, w.environments, w.environment, w.availability) == (900, "NZD", ["dev"], "dev", "high")


def test_currency_defaults_to_the_jurisdiction_and_can_be_set():
    assert wl(residency="uk").currency == "GBP" and wl(residency="eu").currency == "EUR" and wl(residency="us", currency="cad").currency == "CAD"
    with pytest.raises(ValueError):
        wl(currency="XYZ")


def test_the_same_design_is_priced_in_any_currency_at_azures_rate(book):
    d = Design(region="uksouth", components=[comp("app_service", "P1v3", instances=2), comp("postgres", "D2ds_v5")])
    usd = assess(d, wl(currency="USD"), book)
    for cur in ("GBP", "EUR", "AUD", "NZD", "CAD"):
        other = assess(d, wl(currency=cur), book)
        assert other.currency == cur and other.total_month == pytest.approx(usd.total_month * book.fx(cur), rel=0.002)
    assert 0.6 < book.fx("GBP") < 0.95 and 0.75 < book.fx("EUR") < 1.05


@pytest.mark.parametrize(
    "residency,locations,primary,dr",
    [
        ("uk", ["United Kingdom"], "uksouth", "ukwest"),
        ("eu", ["Germany"], "germanywestcentral", "francecentral"),  # Germany North is restricted to in-country customers
        ("eu", ["Ireland"], "northeurope", "westeurope"),
        ("us", ["Texas"], "southcentralus", "northcentralus"),
        ("us", [], "eastus2", "centralus"),
        ("canada", ["Canada"], "canadacentral", "canadaeast"),
        ("australia", ["Australia"], "australiaeast", "australiasoutheast"),
        ("anz", [], "newzealandnorth", "australiaeast"),
        ("nz", [], "newzealandnorth", None),
    ],
)
def test_regions_follow_the_users_and_dr_uses_the_pair(book, residency, locations, primary, dr):
    w = wl(residency=residency, user_locations=locations, availability="critical", pii=True)
    d = architect(w, book)
    assert d.region == primary and d.dr_region == dr
    assert all(c.region in RESIDENCY_REGIONS[residency] for c in d.components if c.service not in ("front_door",))


def test_users_outside_the_jurisdiction_do_not_pull_data_out_of_it():
    assert location_region("Singapore") == "southeastasia"
    assert primary_region(RESIDENCY_REGIONS["eu"], "westeurope", ["Singapore"]) == "westeurope"


def test_every_jurisdiction_is_enforced(book):
    for residency in ("eu", "uk", "us", "canada", "australia", "nz"):
        outside = "eastus2" if residency != "us" else "uksouth"
        d = Design(region=outside, components=[comp("app_service", "S1", outside), comp("postgres", "D2ds_v5", outside)])
        assert "RES-REGION" in {v.rule for v in assess(d, wl(residency=residency), book).violations}, residency


def test_language_model_stays_in_the_eu_zone_but_needs_an_exception_for_the_uk(book):
    eu = wl(needs=["web_app", "llm"], residency="eu", pii=True, user_locations=["Netherlands"], llm_input_mtokens=5)
    d = architect(eu, book)
    llm = next(c for c in d.components if c.service == "azure_openai")
    assert llm.tier == "o4-mini_datazone" and llm.region in RESIDENCY_REGIONS["eu"] and not d.accepted_exceptions
    assert not assess(d, eu, book).violations
    uk = wl(needs=["web_app", "llm"], residency="uk", pii=True, llm_input_mtokens=5)
    d = architect(uk, book)
    assert d.accepted_exceptions and "[RES-AI-ZONE]" in d.accepted_exceptions[0] and not assess(d, uk, book).violations


def test_gdpr_flags_personal_data_sent_to_a_country_without_adequacy(book):
    w = wl(residency="any", frameworks=["gdpr"], pii=True)
    au = Design(region="australiaeast", components=[comp("app_service", "S1", "australiaeast"), comp("postgres", "D2ds_v5", "australiaeast")])
    uk = Design(region="uksouth", components=[comp("app_service", "S1"), comp("postgres", "D2ds_v5")])
    assert "COMP-GDPR-TRANSFER" in rules_of(assess(au, w, book)) and "COMP-GDPR-TRANSFER" not in rules_of(assess(uk, w, book))


def test_zone_redundancy_needs_a_region_with_zones(book):
    d = Design(region="ukwest", components=[comp("app_service", "P1v3", "ukwest", instances=3, zone_redundant=True)])
    assert "REL-ZR-REGION" in rules_of(assess(d, wl(needs=["web_app"]), book))


# ---------------------------------------------------------------- reliability: RTO, RPO and backups
def test_a_short_rto_builds_a_warm_standby(book):
    w = wl(residency="uk", availability="high", rto_minutes=60, pii=True)
    d = architect(w, book)
    in_dr = {c.service for c in d.components if c.region == "ukwest"}
    assert {"app_service", "postgres"} <= in_dr and any(c.service == "front_door" for c in d.components)
    assert "REL-DR" not in rules_of(assess(d, w, book))


def test_a_tight_rpo_alone_builds_a_database_replica_but_no_standby(book):
    w = wl(needs=["api", "relational_db"], residency="eu", user_locations=["Ireland"], rpo_minutes=5, rto_minutes=720)
    d = architect(w, book)
    in_dr = [c for c in d.components if c.region == "westeurope"]
    assert [c.service for c in in_dr] == ["postgres"] and in_dr[0].settings["replica"] == 1
    without = d.model_copy(update={"components": [c for c in d.components if c.region != "westeurope"], "dr_region": None})
    assert "REL-DR" in rules_of(assess(without, w, book))


def test_an_rpo_of_hours_turns_on_geo_redundant_backups(book):
    w = wl(residency="uk", rpo_minutes=240, needs=["web_app", "relational_db", "object_storage"])
    d = architect(w, book)
    db = next(c for c in d.components if c.service == "postgres")
    assert db.settings["geo_backup"] == 1 and any(c.tier == "hot_grs" for c in d.components)
    db.settings.pop("geo_backup")
    assert "REL-GEO-BACKUP" in rules_of(assess(d, w, book))


def test_backups_past_35_days_need_long_term_retention(book):
    w = wl(residency="australia", backup_retention_days=90)
    d = architect(w, book)
    vault = next(c for c in d.components if c.service == "backup_vault")
    assert vault.settings["retention_days"] == 90 and not rules_of(assess(d, w, book)) & {"REL-BACKUP"}
    sql = architect(wl(residency="australia", backup_retention_days=90, constraints=["must use SQL Server"]), book)
    db = next(c for c in sql.components if c.service == "azure_sql")
    assert db.settings["ltr_days"] == 90 and db.settings["ltr_gb"] > 0
    short = d.model_copy(update={"components": [c for c in d.components if c.service != "backup_vault"]})
    assert "REL-BACKUP" in rules_of(assess(short, w, book))


# ---------------------------------------------------------------- data, users and traffic
def test_growth_sizes_storage_for_12_months_and_the_tier_for_36(book):
    small = architect(wl(data_gb=300), book)
    grows = architect(wl(data_gb=300, data_growth_pct=40), book)
    tiers = [next(c.tier for c in d.components if c.service == "postgres") for d in (small, grows)]
    assert tiers == ["D2ds_v5", "D4ds_v5"]
    a = assess(grows, wl(data_gb=300, data_growth_pct=40), book)
    assert a.sizing["data_gb_12m"] == pytest.approx(420) and a.sizing["data_gb_36m"] == pytest.approx(823.2) and a.sizing["db_storage_gb"] == 1024


def test_growth_past_a_tier_limit_is_flagged(book):
    d = Design(region="uksouth", components=[comp("app_service", "S1"), comp("azure_sql", "S2")])
    assert "PERF-CAPACITY" in rules_of(assess(d, wl(data_gb=150, data_growth_pct=30), book))


def test_peak_concurrency_sets_autoscale_and_is_checked(book):
    w = wl(residency="us", peak_concurrent_users=5000, availability="high")
    d = architect(w, book)
    app = next(c for c in d.components if c.service == "app_service")
    assert (app.tier, app.instances, app.settings["max_instances"]) == ("P1v3", 3, 7)  # 800 concurrent users per P1v3 instance
    assert "PERF-INSTANCES" not in rules_of(assess(d, w, book)) and "azurerm_monitor_autoscale_setting" in generate(d, w)
    fixed = Design(region="eastus2", components=[comp("app_service", "S1", "eastus2")])
    assert "PERF-INSTANCES" in rules_of(assess(fixed, w, book))


def test_read_ratio_decides_whether_a_cache_helps(book):
    d = Design(region="uksouth", components=[comp("app_service", "S1"), comp("postgres", "D2ds_v5")])
    assert "PERF-CACHE" in rules_of(assess(d, wl(peak_rps=120, read_ratio=0.9), book))
    assert "PERF-CACHE" not in rules_of(assess(d, wl(peak_rps=120, read_ratio=0.2), book))
    assert not any(c.service == "redis" for c in architect(wl(peak_rps=120, read_ratio=0.2), book).components)


def test_users_on_several_continents_get_an_edge_network(book):
    w = wl(needs=["web_app"], user_locations=["United States", "United Kingdom", "Australia"])
    assert any(c.service == "front_door" for c in architect(w, book).components)
    d = Design(region="eastus2", components=[comp("app_service", "S1", "eastus2")])
    assert "PERF-EDGE" in rules_of(assess(d, w, book))


# ---------------------------------------------------------------- security, compliance, identity
def test_pci_brings_a_waf_private_data_and_a_year_of_logs(book):
    w = wl(residency="us", frameworks=["pci_dss"], needs=["web_app", "relational_db", "object_storage"])
    d = architect(w, book)
    assert w.card_data and w.pii
    assert any(c.service == "app_gateway" for c in d.components)
    assert all(c.private_endpoint for c in d.components if c.service in ("postgres", "blob_storage"))
    assert next(c for c in d.components if c.service == "log_analytics").settings["retention_days"] == 365
    no_waf = d.model_copy(update={"components": [c for c in d.components if c.service != "app_gateway"]})
    waf = next(f for f in assess(no_waf, w, book).findings if f.rule == "SEC-WAF")
    assert waf.severity == "high" and "6.4.2" in waf.source


def test_hipaa_brings_audit_logs_six_years_retention_and_geo_backups(book):
    w = wl(residency="us", frameworks=["hipaa"])
    d = architect(w, book)
    logs = next(c for c in d.components if c.service == "log_analytics")
    assert logs.settings["retention_days"] == 2190 and logs.settings["audit"] == 1
    assert next(c for c in d.components if c.service == "postgres").settings["geo_backup"] == 1
    a = assess(d, w, book)
    assert not a.violations and {"COMP-AUDIT", "COMP-LOGS", "REL-GEO-BACKUP"}.isdisjoint(rules_of(a)) and "COMP-BAA" in rules_of(a)
    logs.settings.pop("audit")
    assert "COMP-AUDIT" in rules_of(assess(d, w, book))


def test_iso_and_soc2_want_a_separate_test_environment(book):
    d = architect(wl(frameworks=["iso27001"]), book)
    assert "COMP-TEST-ENV" in rules_of(assess(d, wl(frameworks=["iso27001"]), book))
    assert "COMP-TEST-ENV" not in rules_of(assess(d, wl(frameworks=["iso27001"], environments=["test", "prod"]), book))


def test_environments_are_priced_as_smaller_copies(book):
    w = wl(residency="uk", availability="high", environments=["dev", "test", "prod"])
    a = assess(architect(w, book), w, book)
    assert set(a.by_environment) == {"dev", "test", "prod"} and a.total_month == pytest.approx(sum(a.by_environment.values()), abs=0.05)
    assert a.by_environment["dev"] < a.by_environment["prod"] / 5


def test_customer_sign_in_adds_external_id_priced_past_the_free_tier(book):
    for users, paid in ((30_000, False), (120_000, True)):
        w = wl(identity="entra_external_id", monthly_users=users)
        a = assess(architect(w, book), w, book)
        cost = sum(line.amount for line in a.lines if line.service == "entra_external_id")
        assert (cost > 0) == paid


def test_integrations_run_as_background_jobs(book):
    w = wl(integrations=["Xero"])
    d = architect(w, book)
    assert any(c.service == "functions" and "Xero" in c.purpose for c in d.components)
    plain = Design(region="eastus2", components=[comp("app_service", "S1", "eastus2"), comp("postgres", "D2ds_v5", "eastus2")])
    assert "OPS-INTEGRATION" in rules_of(assess(plain, w, book))


def test_stack_constraints_choose_and_check_services(book):
    assert stack_rules(["must use PostgreSQL", "no Kubernetes", ".NET team"]) == {
        "must": [("postgres", "must use PostgreSQL")],
        "forbid": [("aks", "no Kubernetes")],
        "prefer": [("azure_sql", ".NET team")],
    }
    dotnet = architect(wl(constraints=[".NET team"]), book)
    assert any(c.service == "azure_sql" for c in dotnet.components)
    w = wl(constraints=["must use PostgreSQL"], needs=["containers", "relational_db"])
    bad = Design(region="eastus2", components=[comp("aks", "standard_d2s", "eastus2"), comp("azure_sql", "S2", "eastus2")])
    assert "STACK-MUST" in {v.rule for v in assess(bad, w, book).violations}
    assert "STACK-FORBID" in {v.rule for v in assess(bad, wl(constraints=["no Kubernetes"], needs=["containers"]), book).violations}


def test_retention_period_deletes_files_and_gdpr_asks_for_one(book):
    w = wl(needs=["web_app", "object_storage"], retention_years=6, residency="uk")
    blob = next(c for c in architect(w, book).components if c.service == "blob_storage")
    assert blob.settings["delete_after_days"] == 2190
    d = architect(wl(frameworks=["gdpr"], pii=True), book)
    assert "COMP-RETENTION" in rules_of(assess(d, wl(frameworks=["gdpr"], pii=True), book))

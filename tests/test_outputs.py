"""Diagrams (Mermaid, draw.io, SVG), the markdown report and the compliance parts of the Terraform."""

import re
from pathlib import Path
from xml.etree import ElementTree

from cloud_architect.__main__ import write_outputs
from cloud_architect.assess import assess
from cloud_architect.diagram import STENCIL, build, drawio, mermaid, svg
from cloud_architect.evaluate import load_examples
from cloud_architect.models import Workload
from cloud_architect.report import report
from cloud_architect.rules import architect
from cloud_architect.terraform import generate

EXAMPLES = dict(load_examples())


def shop():
    return EXAMPLES["hollin-wren-online-shop"]


def test_every_example_designs_cleanly_in_its_own_currency(book):
    currencies = {}
    for name, w in EXAMPLES.items():
        d = architect(w, book)
        a = assess(d, w, book)
        assert not a.violations, (name, [v.message for v in a.violations])
        assert a.total_month <= w.budget_month
        currencies[name] = a.currency
    assert set(currencies.values()) == {"GBP", "USD", "AUD", "EUR"}


def test_the_diagram_shows_regions_dr_zones_private_network_identity_and_flows(book):
    w = shop()
    d = architect(w, book)
    g = build(d, w)
    titles = [gr.title for gr in g.groups]
    assert any(t.startswith("UK South (London) · primary · 3 availability zones") for t in titles)
    assert any(t.startswith("UK West (Cardiff) · disaster recovery") for t in titles)
    assert any(gr.kind == "private" for gr in g.groups) and any(gr.id == "global" for gr in g.groups)
    texts = {e.text for e in g.edges}
    assert {"HTTPS", "sign in", "geo-replication", "sync, retried", "primary", "failover"} <= texts
    m = mermaid(d, w)
    assert m.startswith("flowchart LR") and "subgraph p_uksouth" in m and "-.->|geo-replication|" in m
    assert mermaid(d, w, theme=True).startswith("%%{init:") and "#e6f2ef" in mermaid(d, w, theme=True)


def test_anything_outside_the_residency_boundary_is_drawn_in_red(book):
    w = Workload(name="Claims", needs=["web_app", "llm"], residency="uk", pii=True, llm_input_mtokens=5)
    d = architect(w, book)
    outside = [gr for gr in build(d, w).groups if gr.outside]
    assert len(outside) == 1 and "outside the residency boundary" in outside[0].title
    assert "fill:#fdecea" in mermaid(d, w)


def test_drawio_uses_drawios_own_azure_shapes_and_is_editable(book):
    w = shop()
    xml = drawio(architect(w, book), w)
    root = ElementTree.fromstring(xml)
    cells = root.findall(".//mxCell")
    shapes = {re.search(r"image=img/lib/azure2/([^;]+);", c.get("style", "")).group(1) for c in cells if "img/lib/azure2/" in c.get("style", "")}
    assert {STENCIL["app_service"], STENCIL["postgres"], STENCIL["front_door"], STENCIL["key_vault"]} & shapes
    assert all(s in STENCIL.values() for s in shapes)
    ids = {c.get("id") for c in cells}
    edges = [c for c in cells if c.get("edge") == "1"]
    assert edges and all(e.get("source") in ids and e.get("target") in ids for e in edges)
    assert not any(c.get("parent") not in ids for c in cells if c.get("parent"))


def test_svg_is_a_self_contained_picture(book):
    w = shop()
    root = ElementTree.fromstring(svg(architect(w, book), w))
    assert root.tag.endswith("svg") and "href" not in svg(architect(w, book), w)


def test_report_has_cost_checks_policies_sizing_and_the_diagram(book):
    w = shop()
    d = architect(w, book)
    md = report(d, w, assess(d, w, book), diagram_file="diagram.svg")
    for needle in ("£", "```mermaid", "![Architecture diagram](diagram.svg)", "## Company policy rules enforced", "## Sizing", "No hard violations",
                   "hollin-wren-security-standard.md, section 3", "dev environment"):  # fmt: skip
        assert needle in md, needle


def test_cli_writes_report_diagrams_and_terraform(book, tmp_path):
    w = shop()
    d = architect(w, book)
    files = write_outputs(tmp_path, d, w, assess(d, w, book))
    assert {f.name for f in files} == {"report.md", "diagram.svg", "diagram.drawio", "diagram.mmd", "main.tf", "design.json"}
    assert all(Path(f).stat().st_size > 100 for f in files)


def test_terraform_carries_the_compliance_settings(book):
    w = shop()
    tf = generate(architect(w, book), w)
    for needle in (
        "backup_retention_days         = 35",
        "geo_redundant_backup_enabled  = true",
        "auto_grow_enabled             = true",
        "geo_backup_key_vault_key_id",  # customer-managed key plus geo backup needs a key in the paired region too
        'resource "azurerm_key_vault" "cmk_geo"',
        'resource "azurerm_storage_management_policy"',
        "delete_after_days_since_modification_greater_than = 2190",
        'resource "azurerm_monitor_diagnostic_setting"',
        "retention_in_days = 90",
        "total_retention_in_days = 365",
        'variable "required_tags"',
        "rbac_authorization_enabled = true",
    ):
        assert re.search(re.escape(needle).replace(r"\ ", r"\s+"), tf), needle


def test_long_backups_and_sql_retention_in_terraform(book):
    tele = EXAMPLES["lumenvale-telehealth"]
    tf = generate(architect(tele, book), tele)
    assert "azurerm_data_protection_backup_policy_postgresql_flexible_server" in tf and 'duration        = "P90D"' in tf
    w = Workload(
        name="Ledger", needs=["api", "relational_db"], constraints=["must use SQL Server"], backup_retention_days=400, geo_backup=True, residency="uk"
    )
    tf = generate(architect(w, book), w)
    assert 'weekly_retention = "P58W"' in tf and 'storage_account_type = "Geo"' in tf

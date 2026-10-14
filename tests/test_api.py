import base64
from pathlib import Path
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from cloud_architect import api
from cloud_architect.config import Settings

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = {  # written the older way, with an NZD budget, to show old clients still work
    "name": "Clinic portal",
    "needs": ["web_app", "relational_db"],
    "residency": "nz",
    "availability": "high",
    "pii": True,
    "budget_nzd_month": 4000,
}
OFFLINE = Settings(llm_api_key="", llm_base_url="https://openrouter.ai/api/v1")


def client(monkeypatch, book, cfg=OFFLINE):
    monkeypatch.setattr(api, "book", lambda: book)
    monkeypatch.setattr(api, "settings", lambda: cfg)
    return TestClient(api.app)


def upload(path: Path) -> dict:
    return {"filename": path.name, "content_b64": base64.b64encode(path.read_bytes()).decode()}


def test_rules_design_returns_price_review_diagrams_terraform_and_report(monkeypatch, book):
    r = client(monkeypatch, book).post("/api/design", json={"workload": WORKLOAD, "mode": "rules"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_month"] > 0 and body["currency"] == "NZD" and body["budget_month"] == 4000
    assert body["diagram"].startswith("flowchart") and "azurerm_resource_group" in body["terraform"]
    assert all(c["region"] == "newzealandnorth" for c in body["design"]["components"])
    assert ElementTree.fromstring(body["drawio"]).tag == "mxfile" and ElementTree.fromstring(body["svg"]).tag.endswith("svg")
    assert "img/lib/azure2/" in body["drawio"] and "NZ$" in body["report"]


def test_agent_mode_falls_back_to_rules_when_the_model_fails(monkeypatch, book):
    def boom(*a, **k):
        raise RuntimeError("402 out of credit")

    monkeypatch.setattr(api, "design_with_tools", boom)
    body = client(monkeypatch, book, Settings(llm_api_key="x")).post("/api/design", json={"workload": WORKLOAD, "mode": "agent"}).json()
    assert body["mode"] == "rules" and "402" in body["note"]


def test_invalid_workload_is_rejected(monkeypatch, book):
    r = client(monkeypatch, book).post("/api/design", json={"workload": {"name": "x", "needs": []}, "mode": "rules"})
    assert r.status_code == 422


def test_presets_options_and_ui(monkeypatch, book):
    c = client(monkeypatch, book)
    presets = c.get("/api/presets").json()
    assert len(presets) == 24 and presets[0]["group"] == "Real-life examples"
    options = c.get("/api/options").json()
    assert "GBP" in options["currencies"] and options["regions"]["uksouth"]["pair"] == "ukwest"
    assert "Cloud Architect" in c.get("/").text


def test_policy_upload_proposes_rules_without_a_model_and_says_so(monkeypatch, book):
    body = client(monkeypatch, book).post("/api/policies/extract", json=upload(ROOT / "examples/policies/hollin-wren-security-standard.md")).json()
    assert body["method"] == "rules" and "No language model" in body["note"]
    checks = {r["check"] for r in body["rules"]}
    assert {"allowed_regions", "customer_managed_keys", "max_monthly_spend"} <= checks
    assert all(r["status"] == "proposed" and r["quote"] for r in body["rules"])


def test_brief_upload_proposes_form_values(monkeypatch, book):
    body = client(monkeypatch, book).post("/api/brief/extract", json=upload(ROOT / "examples/briefs/falkenhain-supplier-portal-rfp.md")).json()
    fields = {f["field"]: f["value"] for f in body["fields"]}
    assert fields["residency"] == "eu" and fields["currency"] == "EUR" and fields["rto_minutes"] == 240


def test_uploads_are_checked(monkeypatch, book):
    c = client(monkeypatch, book)
    assert c.post("/api/policies/extract", json={"filename": "virus.exe", "content_b64": "AA=="}).status_code == 415
    assert c.post("/api/policies/extract", json={"filename": "p.md", "content_b64": "not base64!"}).status_code == 422


def test_accepted_policies_are_enforced_and_cited(monkeypatch, book):
    rule = {
        "check": "allowed_regions", "regions": ["uksouth", "ukwest"], "status": "accepted", "id": "P1",
        "source": "standard.md", "section": "3", "quote": "Data must be stored only in the United Kingdom.",
    }  # fmt: skip
    design = {"region": "westeurope", "components": [{"service": "app_service", "tier": "S1", "region": "westeurope"}]}
    workload = {"name": "Shop", "needs": ["web_app"], "policies": [rule]}
    body = client(monkeypatch, book).post("/api/assess", json={"workload": workload, "design": design}).json()
    v = next(v for v in body["violations"] if v["rule"] == "POL-REGION")
    assert "United Kingdom" in v["source"] and "standard.md, section 3" in v["source"]

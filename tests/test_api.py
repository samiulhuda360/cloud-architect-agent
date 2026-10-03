from fastapi.testclient import TestClient

from cloud_architect import api
from cloud_architect.config import Settings

WORKLOAD = {
    "name": "Clinic portal",
    "needs": ["web_app", "relational_db"],
    "residency": "nz",
    "availability": "high",
    "pii": True,
    "budget_nzd_month": 4000,
}


def client(monkeypatch, book):
    monkeypatch.setattr(api, "book", lambda: book)
    return TestClient(api.app)


def test_rules_design_returns_price_review_diagram_and_terraform(monkeypatch, book):
    r = client(monkeypatch, book).post("/api/design", json={"workload": WORKLOAD, "mode": "rules"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_nzd_month"] > 0 and body["diagram"].startswith("flowchart") and "azurerm_resource_group" in body["terraform"]
    assert all(c["region"] == "newzealandnorth" for c in body["design"]["components"])


def test_agent_mode_falls_back_to_rules_when_the_model_fails(monkeypatch, book):
    def boom(*a, **k):
        raise RuntimeError("402 out of credit")

    monkeypatch.setattr(api, "design_with_tools", boom)
    monkeypatch.setattr(api, "settings", lambda: Settings(llm_api_key="x"))  # pretend a model is configured
    body = client(monkeypatch, book).post("/api/design", json={"workload": WORKLOAD, "mode": "agent"}).json()
    assert body["mode"] == "rules" and "402" in body["note"]


def test_invalid_workload_is_rejected(monkeypatch, book):
    r = client(monkeypatch, book).post("/api/design", json={"workload": {"name": "x", "needs": []}, "mode": "rules"})
    assert r.status_code == 422


def test_presets_and_ui(monkeypatch, book):
    c = client(monkeypatch, book)
    assert len(c.get("/api/presets").json()) == 20
    assert "Cloud Architect" in c.get("/").text

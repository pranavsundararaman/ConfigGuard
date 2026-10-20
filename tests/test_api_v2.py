from fastapi.testclient import TestClient

from app import main


client = TestClient(main.app)


def test_configuration_history_endpoints(monkeypatch):
    versions = [{"id": "v1", "project_id": "payments", "configuration": {"timeout": 10}}]
    monkeypatch.setattr(main, "save_configuration_version", lambda project, config, fmt: "v1")
    monkeypatch.setattr(main, "get_configuration_version", lambda project, version: versions[0])
    monkeypatch.setattr(main, "list_configuration_versions", lambda project, limit: versions)

    saved = client.post(
        "/api/projects/payments/config-versions",
        json={"configuration": {"timeout": 10}, "format": "json"},
    )
    history = client.get("/api/projects/payments/config-versions")

    assert saved.status_code == 201
    assert saved.json()["id"] == "v1"
    assert history.status_code == 200
    assert history.json()[0]["configuration"] == {"timeout": 10}


def test_historical_analysis_endpoint_saves_and_links_records(monkeypatch):
    history = [
        {"id": f"v{i}", "configuration": {"retries": retries}}
        for i, retries in enumerate([2, 2, 3, 2], start=1)
    ]
    saved_analysis = {}
    monkeypatch.setattr(main, "list_configuration_versions", lambda project, limit: history)
    monkeypatch.setattr(main, "save_configuration_version", lambda project, config, fmt: "proposed-v")

    def save_analysis(project, result):
        saved_analysis.update(result)
        return "analysis-1"

    monkeypatch.setattr(main, "save_analysis", save_analysis)
    response = client.post(
        "/api/projects/payments/historical-analysis",
        json={"configuration": {"retries": 100}, "min_observations": 3},
    )

    assert response.status_code == 201
    assert response.json()["id"] == "analysis-1"
    assert response.json()["results"][0]["status"] == "unusual"
    assert saved_analysis["project_id"] == "payments"
    assert saved_analysis["proposed_version_id"] == "proposed-v"
    assert saved_analysis["baseline_version_ids"] == ["v1", "v2", "v3", "v4"]


def test_project_analysis_retrieval_is_project_scoped(monkeypatch):
    monkeypatch.setattr(main, "list_project_analyses", lambda project, limit: [{"id": "a1", "project_id": project}])
    monkeypatch.setattr(main, "fetch_project_analysis", lambda project, analysis: {"id": analysis, "project_id": project})

    listed = client.get("/api/projects/payments/analyses")
    detail = client.get("/api/projects/payments/analyses/a1")

    assert listed.status_code == 200
    assert listed.json()[0]["project_id"] == "payments"
    assert detail.status_code == 200
    assert detail.json()["id"] == "a1"


def test_existing_v1_health_endpoint_is_unchanged():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "configguard"}

import pytest
from fastapi.testclient import TestClient

from app import main, persistence
from tests.test_persistence import FakeDatabase


client = TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def fake_database(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    return database


def test_configuration_save_then_retrieve_workflow(fake_database):
    saved = client.post(
        "/api/projects/payments/config-versions",
        json={"configuration": {"timeout": 30}, "format": "json"},
    )
    version_id = saved.json()["id"]

    history = client.get("/api/projects/payments/config-versions")
    detail = client.get(f"/api/projects/payments/config-versions/{version_id}")

    assert saved.status_code == 201
    assert saved.json()["lifecycle"] == "proposed"
    assert history.json()[0]["configuration"] == {"timeout": 30}
    assert detail.json()["id"] == version_id


def test_project_and_legacy_retrieval_routes_are_scoped(fake_database):
    saved = client.post(
        "/api/projects/alpha/config-versions",
        json={"configuration": {"timeout": 30}, "format": "json"},
    ).json()
    analysis_id = persistence.save_analysis("alpha", {"classification": "SAFE"})

    assert client.get(f"/api/projects/beta/config-versions/{saved['id']}").status_code == 404
    assert client.get(f"/api/projects/beta/analyses/{analysis_id}").status_code == 404
    assert client.get(f"/api/config-versions/{saved['id']}?project_id=beta").status_code == 404
    assert client.get(f"/api/analyses/{analysis_id}?project_id=beta").status_code == 404
    assert client.get("/api/config-versions").status_code == 422
    assert client.get("/api/analyses").status_code == 422


def test_historical_analysis_only_uses_deployed_versions_and_does_not_promote_proposal(fake_database):
    version_ids = []
    for timeout in [2, 2, 4]:
        saved = client.post(
            "/api/projects/payments/config-versions",
            json={"configuration": {"timeout": timeout}, "format": "json"},
        ).json()
        version_ids.append(saved["id"])

    before_deploy = client.post(
        "/api/projects/payments/historical-analysis",
        json={"configuration": {"timeout": 3}},
    ).json()
    assert before_deploy["results"][0]["status"] == "insufficient_history"
    assert before_deploy["results"][0]["observation_count"] == 0

    for version_id in version_ids:
        response = client.post(f"/api/projects/payments/config-versions/{version_id}/deploy")
        assert response.status_code == 200
        assert response.json()["lifecycle"] == "deployed"

    first_analysis = client.post(
        "/api/projects/payments/historical-analysis",
        json={"configuration": {"timeout": 3}},
    ).json()
    second_analysis = client.post(
        "/api/projects/payments/historical-analysis",
        json={"configuration": {"timeout": 3}},
    ).json()

    assert first_analysis["results"][0]["observation_count"] == 3
    assert set(first_analysis["baseline_version_ids"]) == set(version_ids)
    assert second_analysis["baseline_version_ids"] == first_analysis["baseline_version_ids"]
    assert first_analysis["proposed_version_id"] != second_analysis["proposed_version_id"]


def test_v1_analyze_preserves_response_and_persists_linked_lifecycle_records(fake_database):
    response = client.post(
        "/api/analyze",
        files={
            "old_config": ("before.json", '{"timeout": 30}', "application/json"),
            "new_config": ("after.json", '{"timeout": 45}', "application/json"),
        },
    )

    assert response.status_code == 200
    assert set(response.json()) == {"risk_score", "classification", "changes", "reasons"}
    versions = fake_database["config_versions"].documents
    analysis = fake_database["risk_analyses"].documents[0]
    assert [version["lifecycle"] for version in versions] == ["deployed", "proposed"]
    assert analysis["before_version_id"] == str(versions[0]["_id"])
    assert analysis["after_version_id"] == str(versions[1]["_id"])
    assert fake_database["write_operations"].documents[0]["state"] == "committed"


@pytest.mark.parametrize("endpoint", ["v1", "historical"])
def test_route_partial_write_failure_leaves_no_inconsistent_versions(fake_database, endpoint):
    fake_database["risk_analyses"].fail_insert = True

    if endpoint == "v1":
        response = client.post(
            "/api/analyze",
            files={
                "old_config": ("before.json", '{"timeout": 30}', "application/json"),
                "new_config": ("after.json", '{"timeout": 45}', "application/json"),
            },
        )
    else:
        response = client.post(
            "/api/projects/payments/historical-analysis",
            json={"configuration": {"timeout": 45}},
        )

    assert response.status_code == 500
    assert fake_database["config_versions"].documents == []
    assert fake_database["risk_analyses"].documents == []
    assert fake_database["write_operations"].documents[0]["state"] == "rolled_back"


def test_historical_minimum_is_validated_at_api_boundary(fake_database):
    response = client.post(
        "/api/projects/payments/historical-analysis",
        json={"configuration": {"timeout": 10}, "min_observations": 2},
    )
    assert response.status_code == 422


def test_database_backed_endpoint_reports_missing_configuration(monkeypatch):
    from app import database

    monkeypatch.setattr(database, "_database", None)
    monkeypatch.setattr(database, "client", None)
    monkeypatch.setattr(persistence, "get_database", database.get_database)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    response = client.get("/api/projects/payments/config-versions")

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "MongoDB is not configured. Set MONGODB_URI before using this operation."
    )


def test_existing_v1_health_endpoint_is_unchanged():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "configguard"}

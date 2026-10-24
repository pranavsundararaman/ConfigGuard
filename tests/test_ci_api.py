import pytest
from fastapi.testclient import TestClient

from app import main, persistence
from tests.test_persistence import FakeDatabase


client = TestClient(main.app, raise_server_exceptions=False)
TOKEN = "synthetic-ci-token-for-tests-only"
READ_TOKEN = "synthetic-ci-read-token-for-tests-only"


@pytest.fixture
def ci_client(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    monkeypatch.setenv("CONFIGGUARD_CI_TOKEN", TOKEN)
    monkeypatch.setenv("CONFIGGUARD_CI_READ_TOKEN", READ_TOKEN)
    return database


def report(**overrides):
    payload = {
        "repository": "owner/service",
        "pull_request_number": 27,
        "pull_request_url": "https://github.com/owner/service/pull/27",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "risk_score": 74.5,
        "classification": "HIGH_RISK",
        "findings": [{
            "file_path": "config/API_KEY_synthetic_path_value.yaml",
            "parameter": "PASSWORD",
            "change_type": "MODIFIED",
            "rule_names": ["Large retry increase"],
        }],
        "change_counts": {
            "files_analyzed": 1,
            "files_unchanged": 0,
            "files_unavailable": 0,
            "files_unsupported": 0,
            "configuration_changes": 1,
            "rule_findings": 1,
        },
    }
    payload.update(overrides)
    return payload


def auth_headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def read_headers(token=READ_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_ci_ingestion_is_authenticated_sanitized_and_idempotent(ci_client):
    response = client.post("/api/projects/payments/ci-analyses", json=report(), headers=auth_headers())

    assert response.status_code == 200
    saved = response.json()["analysis"]
    assert response.json()["created"] is True
    assert saved["project_id"] == "payments"
    assert saved["findings"][0]["file_path"] == "config/[redacted-name].yaml"
    assert saved["findings"][0]["parameter"] == "[redacted-name]"
    assert "synthetic_path_value" not in response.text
    assert "old_value" not in saved and "new_value" not in saved
    assert "configuration" not in saved and "source_excerpt" not in saved

    retry = client.post("/api/projects/payments/ci-analyses", json=report(), headers=auth_headers())
    assert retry.status_code == 200
    assert retry.json()["created"] is False
    assert retry.json()["analysis"]["id"] == saved["id"]
    assert len(ci_client["ci_analyses"].documents) == 1
    indexes = ci_client["ci_analyses"].indexes
    assert indexes[0][1]["unique"] is True


def test_ci_ingestion_and_reads_reject_missing_or_invalid_tokens(ci_client, monkeypatch):
    missing = client.post("/api/projects/payments/ci-analyses", json=report())
    invalid = client.post("/api/projects/payments/ci-analyses", json=report(), headers=auth_headers("wrong-token"))
    assert missing.status_code == invalid.status_code == 401
    assert missing.json() == invalid.json() == {"detail": "CI integration authentication failed."}

    read_with_ingest_token = client.get("/api/projects/payments/ci-analyses", headers=auth_headers())
    assert read_with_ingest_token.status_code == 401

    monkeypatch.delenv("CONFIGGUARD_CI_READ_TOKEN")
    unavailable = client.get("/api/projects/payments/ci-analyses", headers=read_headers())
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "CI integration is not configured."}


@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_score": 101},
        {"classification": "DEPLOYMENT_WILL_FAIL"},
        {"classification": "SAFE"},
        {"pull_request_url": "https://github.com/other/repo/pull/27"},
        {"pull_request_url": "https://github.com/owner/service/pull/27?synthetic-secret"},
        {"base_sha": "not-a-commit"},
        {"extra_secret_field": "synthetic-unaccepted-value"},
    ],
)
def test_ci_ingestion_rejects_invalid_or_value_bearing_payloads_without_echo(ci_client, overrides):
    response = client.post("/api/projects/payments/ci-analyses", json=report(**overrides), headers=auth_headers())

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid CI analysis payload."}
    assert "synthetic-unaccepted-value" not in response.text


def test_ci_ingestion_rejects_oversized_request_body(ci_client):
    response = client.post(
        "/api/projects/payments/ci-analyses",
        content=b" " * (64 * 1024 + 1),
        headers={**auth_headers(), "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "CI analysis payload is too large."}


def test_ci_storage_errors_are_generic_and_do_not_echo_database_details(ci_client, monkeypatch):
    fake_database_detail = "synthetic-mongo-credential-must-not-echo"

    def fail_storage(*args, **kwargs):
        raise RuntimeError(f"database connection contains {fake_database_detail}")

    monkeypatch.setattr(main, "upsert_ci_analysis", fail_storage)
    response = client.post("/api/projects/payments/ci-analyses", json=report(), headers=auth_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "CI analysis storage is unavailable."}
    assert fake_database_detail not in response.text


def test_ci_history_reads_are_authenticated_and_project_scoped(ci_client):
    record = client.post("/api/projects/alpha/ci-analyses", json=report(), headers=auth_headers()).json()["analysis"]

    listing = client.get("/api/projects/alpha/ci-analyses", headers=read_headers())
    detail = client.get(f"/api/projects/alpha/ci-analyses/{record['id']}", headers=read_headers())
    other_project_list = client.get("/api/projects/beta/ci-analyses", headers=read_headers())
    other_project_detail = client.get(f"/api/projects/beta/ci-analyses/{record['id']}", headers=read_headers())

    assert listing.status_code == detail.status_code == 200
    assert [item["id"] for item in listing.json()] == [record["id"]]
    assert detail.json()["repository"] == "owner/service"
    assert other_project_list.json() == []
    assert other_project_detail.status_code == 404
    assert client.get("/api/projects/alpha/ci-analyses").status_code == 401


def test_ci_openapi_documents_the_bounded_payload_and_bearer_auth():
    operation = client.get("/openapi.json").json()["paths"]["/api/projects/{project_id}/ci-analyses"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert "security" in operation
    assert request_schema["additionalProperties"] is False
    assert "configuration" not in request_schema.get("properties", {})
    assert "old_value" not in request_schema.get("properties", {})


def test_ci_report_rejects_oversized_findings_and_unknown_rules(ci_client):
    many_findings = [{
        "file_path": "config.json",
        "parameter": "service.timeout",
        "change_type": "MODIFIED",
        "rule_names": [],
    }] * 501
    too_many = client.post(
        "/api/projects/payments/ci-analyses",
        json=report(findings=many_findings),
        headers=auth_headers(),
    )
    unknown_rule = client.post(
        "/api/projects/payments/ci-analyses",
        json=report(findings=[{
            "file_path": "config.json",
            "parameter": "timeout",
            "change_type": "MODIFIED",
            "rule_names": ["untrusted synthetic value"],
        }]),
        headers=auth_headers(),
    )
    assert too_many.status_code == 422
    assert unknown_rule.status_code == 422
    assert "untrusted synthetic value" not in unknown_rule.text

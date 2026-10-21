import subprocess
from pathlib import Path

import pytest

from app import ci_analysis
from app.ci_analysis import (
    analyze_changed_file,
    changed_config_paths,
    detect_format,
    main,
    render_summary,
    run,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci-test@example.invalid")
    _git(repo, "config", "user.name", "CI test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    return repo, base


def _commit(repo: Path, message: str = "head") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("config.json", "json"),
        ("nested/config.yaml", "yaml"),
        ("nested/config.YML", "yml"),
        (".env", "env"),
        (".env.production", "env"),
        (".env.staging", "env"),
        ("settings.env", "env"),
        ("config.toml", None),
        ("infra/main.tf", None),
        (".envrc", None),
    ],
)
def test_detects_supported_and_unsupported_config_formats(path, expected):
    assert detect_format(path) == expected


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("service.json", '{"MAX_RETRIES": 20}'),
        ("service.yaml", "MAX_RETRIES: 20\n"),
        ("service.yml", "MAX_RETRIES: 20\n"),
        (".env", "MAX_RETRIES=20\n"),
        (".env.production", "MAX_RETRIES=20\n"),
        (".env.staging", "MAX_RETRIES=20\n"),
        ("settings.env", "MAX_RETRIES=20\n"),
    ],
)
def test_analyzes_valid_formats_including_dotenv_variants(git_repo, path, content):
    repo, base = git_repo
    filepath = repo / path
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    head = _commit(repo)

    changed = changed_config_paths(str(repo), base, head)[0]
    result = analyze_changed_file(str(repo), base, head, changed.status, changed.path, changed.previous_path)

    assert result.status == "analyzed"
    assert result.change_count == 1


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("broken.json", '{"SECRET":"json_secret_value",'),
        ("broken.yaml", "service: [unterminated-secret-value\n"),
        (".env.production", "NOT AN ASSIGNMENT secret_env_value\n"),
    ],
)
def test_malformed_formats_are_reported_without_parser_details(git_repo, path, content, capsys):
    repo, base = git_repo
    (repo / path).write_text(content, encoding="utf-8")
    head = _commit(repo)
    results = run(base, head, str(repo))

    assert results[0].status == "unavailable"
    assert results[0].message.startswith("Could not analyze:")
    summary = render_summary(results)
    assert "json_secret_value" not in summary
    assert "unterminated-secret-value" not in summary
    assert "secret_env_value" not in summary
    captured = capsys.readouterr()
    assert "json_secret_value" not in captured.out + captured.err
    assert "unterminated-secret-value" not in captured.out + captured.err
    assert "secret_env_value" not in captured.out + captured.err


def test_added_and_deleted_files_are_compared_with_an_empty_configuration(git_repo):
    repo, base = git_repo
    (repo / "removed.json").write_text('{"before": 1}', encoding="utf-8")
    base = _commit(repo, "add old config")
    (repo / "removed.json").unlink()
    (repo / "added.yaml").write_text("after: 2\n", encoding="utf-8")
    head = _commit(repo, "replace configs")

    results = run(base, head, str(repo))

    by_path = {result.path: result for result in results}
    assert set(by_path) == {"removed.json", "added.yaml"}
    assert all(result.status == "analyzed" for result in results)
    assert all(result.change_count == 1 for result in results)


def test_semantically_unchanged_configuration_is_reported_as_unchanged(git_repo):
    repo, base = git_repo
    (repo / "service.json").write_text('{"service":{"timeout":30}}\n', encoding="utf-8")
    base = _commit(repo, "add config")
    (repo / "service.json").write_text('{\n  "service": { "timeout": 30 }\n}\n', encoding="utf-8")
    head = _commit(repo, "format config")

    result = run(base, head, str(repo))[0]

    assert result.status == "unchanged"
    assert result.change_count == 0
    assert "No semantic" in render_summary([result])


def test_rename_compares_old_and_new_file_as_one_pair(git_repo):
    repo, base = git_repo
    old_path = repo / "before.json"
    old_path.write_text('{"MAX_RETRIES":3}\n', encoding="utf-8")
    base = _commit(repo, "add original config")
    new_path = repo / "after.json"
    old_path.rename(new_path)
    head = _commit(repo, "rename config")

    changes = changed_config_paths(str(repo), base, head)
    assert len(changes) == 1
    assert changes[0].status == "R"
    assert changes[0].previous_path == "before.json"
    assert changes[0].path == "after.json"
    result = run(base, head, str(repo))[0]
    assert result.status == "unchanged"
    assert result.change_count == 0
    assert "before.json -&gt; after.json" in render_summary([result])


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("secret.json", '{"TOKEN":"jsonBeforeSecret"}', '{"TOKEN":"jsonAfterSecret"}'),
        ("secret.yaml", "TOKEN: yamlBeforeSecret\n", "TOKEN: yamlAfterSecret\n"),
        (".env.staging", "TOKEN=envBeforeSecret\n", "TOKEN=envAfterSecret\n"),
    ],
)
def test_valid_config_summaries_redact_synthetic_secrets(git_repo, path, before, after):
    repo, base = git_repo
    file_path = repo / path
    file_path.write_text(before, encoding="utf-8")
    base = _commit(repo, "add secret fixture")
    file_path.write_text(after, encoding="utf-8")
    head = _commit(repo, "change secret fixture")

    summary = render_summary(run(base, head, str(repo)))

    assert "jsonBeforeSecret" not in summary
    assert "jsonAfterSecret" not in summary
    assert "yamlBeforeSecret" not in summary
    assert "yamlAfterSecret" not in summary
    assert "envBeforeSecret" not in summary
    assert "envAfterSecret" not in summary


def test_unsupported_configuration_is_reported_without_analysis(git_repo):
    repo, base = git_repo
    (repo / "config.toml").write_text("timeout = 30\n", encoding="utf-8")
    head = _commit(repo)

    result = run(base, head, str(repo))[0]

    assert result.status == "unsupported"
    summary = render_summary([result])
    assert "Unsupported" in summary
    assert "**Unsupported:** 1" in summary


def test_summary_distinguishes_clean_analysis_from_unanalyzed_files():
    clean = render_summary([
        ci_analysis.FileAnalysis("same.json", "unchanged", classification="SAFE", risk_score=0.0, message="No semantic configuration changes detected."),
    ])
    blocked = render_summary([
        ci_analysis.FileAnalysis("broken.yaml", "unavailable", message="Could not analyze: invalid or unreadable configuration."),
    ])

    assert "No rule-based findings" in clean
    assert "No files were analyzed successfully" in blocked
    assert "No files were analyzed successfully" not in clean


def test_summary_has_findings_counts_and_classification_but_no_values(git_repo):
    repo, base = git_repo
    path = repo / ".env.production"
    path.write_text("MAX_RETRIES=3\nPASSWORD=beforeSecretValue\n", encoding="utf-8")
    base = _commit(repo, "add baseline")
    path.write_text("MAX_RETRIES=20\nPASSWORD=afterSecretValue\n", encoding="utf-8")
    head = _commit(repo, "change configuration")

    results = run(base, head, str(repo))
    summary = render_summary(results)

    assert "REVIEW_REQUIRED" in summary or "HIGH_RISK" in summary
    assert "Large retry increase" in summary
    assert "Rule findings:" in summary
    assert "beforeSecretValue" not in summary
    assert "afterSecretValue" not in summary
    assert "MAX_RETRIES" in summary


def test_malformed_input_and_risk_findings_exit_successfully_in_advisory_mode(git_repo, tmp_path, monkeypatch):
    repo, base = git_repo
    (repo / "broken.json").write_text('{"SECRET":"must_not_leak",', encoding="utf-8")
    head = _commit(repo)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    exit_code = main(["--base", base, "--head", head, "--repo", str(repo)])

    assert exit_code == 0
    summary = summary_file.read_text(encoding="utf-8")
    assert "Could not analyze" in summary
    assert "must_not_leak" not in summary


def test_high_risk_finding_does_not_fail_cli(git_repo, tmp_path, monkeypatch):
    repo, base = git_repo
    config = repo / "service.json"
    config.write_text('{"MAX_RETRIES":3}', encoding="utf-8")
    base = _commit(repo, "add base config")
    config.write_text('{"MAX_RETRIES":30}', encoding="utf-8")
    head = _commit(repo, "raise retries")
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    exit_code = main(["--base", base, "--head", head, "--repo", str(repo)])

    assert exit_code == 0
    assert "Large retry increase" in summary_file.read_text(encoding="utf-8")


def test_non_configuration_changes_do_not_appear_in_results(git_repo):
    repo, base = git_repo
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    head = _commit(repo)

    assert changed_config_paths(str(repo), base, head) == []


def test_pull_request_workflow_uses_event_shas_full_history_and_read_only_access():
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "configguard-pr.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "types: [opened, synchronize, reopened]" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "contents: write" not in workflow
    assert "pull-requests: write" not in workflow


def test_git_comparison_failure_is_a_nonzero_cli_error(git_repo, tmp_path, monkeypatch, capsys):
    repo, _ = git_repo
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    exit_code = main(["--base", "missing-base-ref", "--head", "missing-head-ref", "--repo", str(repo)])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Unable to read the requested Git comparison." in captured.err
    assert "missing-base-ref" not in captured.err


def test_git_blob_read_failure_is_not_downgraded_to_an_advisory(git_repo, tmp_path, monkeypatch, capsys):
    repo, base = git_repo
    (repo / "config.json").write_text('{"TOKEN":"must_not_leak"}', encoding="utf-8")
    head = _commit(repo)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    original_git = ci_analysis._git

    def fail_blob_read(git_repo_path, *args):
        if args and args[0] == "show":
            raise ci_analysis.GitCommandError("Unable to read the requested Git comparison.")
        return original_git(git_repo_path, *args)

    monkeypatch.setattr(ci_analysis, "_git", fail_blob_read)
    exit_code = main(["--base", base, "--head", head, "--repo", str(repo)])

    assert exit_code == 2
    assert "Unable to read the requested Git comparison." in capsys.readouterr().err
    assert not summary_file.exists()

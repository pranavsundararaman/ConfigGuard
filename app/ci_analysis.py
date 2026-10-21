"""Runner-side pull request configuration analysis using ConfigGuard's V1 engine."""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from .parser import parse_config
from .risk_engine import assess_risk


_UNSUPPORTED_CONFIG_SUFFIXES = {
    ".cfg", ".conf", ".config", ".envrc", ".hcl", ".ini", ".jsonc",
    ".properties", ".tf", ".tfvars", ".toml", ".xml",
}
_SENSITIVE_PATH_COMPONENT = re.compile(r"(?i)(api[-_]?key|password|token|secret|credential)")


@dataclass(frozen=True)
class FileAnalysis:
    path: str
    status: str
    classification: str = "-"
    risk_score: float | None = None
    change_count: int = 0
    rule_findings: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: str
    previous_path: str | None = None


class GitCommandError(RuntimeError):
    """A sanitized error indicating the Git comparison could not be completed."""


def detect_format(path: str) -> str | None:
    """Return a supported format, including common dotenv filenames."""
    name = PurePosixPath(path).name.lower()
    suffix = PurePosixPath(name).suffix
    if name == ".env" or name.startswith(".env.") or suffix == ".env":
        return "env"
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return suffix[1:]
    return None


def is_config_candidate(path: str) -> bool:
    if detect_format(path):
        return True
    name = PurePosixPath(path).name.lower()
    return PurePosixPath(name).suffix in _UNSUPPORTED_CONFIG_SUFFIXES


def _git(repo: str, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", repo, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise GitCommandError("Unable to run the requested Git comparison.") from error
    if completed.returncode:
        # Git diagnostics can include filenames or ref text. Keep them out of CI logs.
        raise GitCommandError("Unable to read the requested Git comparison.")
    return completed.stdout


def changed_config_paths(repo: str, base: str, head: str) -> list[ChangedPath]:
    """Return changed configuration paths from the base-to-head diff."""
    raw = _git(repo, "diff", "--find-renames", "--name-status", "-z", f"{base}...{head}", "--")
    fields = raw.decode("utf-8", errors="replace").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    entries: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if index >= len(fields):
            raise GitCommandError("Git returned an incomplete file-change list.")
        previous_path = None
        if status.startswith(("R", "C")):
            previous_path = fields[index]
            index += 1
            if index >= len(fields):
                raise GitCommandError("Git returned an incomplete file-change list.")
            path = fields[index]
            index += 1
        else:
            path = fields[index]
            index += 1
        if is_config_candidate(path) or (previous_path is not None and is_config_candidate(previous_path)):
            entries.append(ChangedPath(status[0], path, previous_path))
    return entries


def _file_at_ref(repo: str, ref: str, path: str) -> bytes:
    return _git(repo, "show", f"{ref}:{path}")


def _parse(path: str, content: bytes) -> dict[str, Any]:
    text = content.decode("utf-8")
    config_format = detect_format(path)
    if config_format is None:
        raise ValueError("unsupported")
    # Treat dotenv-style names consistently without broadening parse_config's API behavior.
    parser_filename = ".env" if config_format == "env" else path
    return parse_config(parser_filename, text, strict_env=config_format == "env")


def analyze_changed_file(
    repo: str,
    base: str,
    head: str,
    status: str,
    path: str,
    previous_path: str | None = None,
) -> FileAnalysis:
    display_path = f"{previous_path} -> {path}" if previous_path else path
    config_format = detect_format(path)
    if config_format is None:
        old_format = detect_format(previous_path) if previous_path else None
        if old_format is None:
            return FileAnalysis(display_path, "unsupported", message="Unsupported configuration format; not analyzed.")
        return FileAnalysis(display_path, "unavailable", message="Could not analyze: renamed to an unsupported configuration format.")

    old_data: dict[str, Any] = {}
    new_data: dict[str, Any] = {}
    try:
        if status not in {"A", "C"}:
            old_data = _parse(previous_path or path, _file_at_ref(repo, base, previous_path or path))
        if status != "D":
            new_data = _parse(path, _file_at_ref(repo, head, path))
    except (UnicodeDecodeError, ValueError, TypeError, yaml.YAMLError):
        # Deliberately omit parser messages and source snippets; they can contain secrets.
        return FileAnalysis(display_path, "unavailable", message="Could not analyze: invalid or unreadable configuration.")
    except GitCommandError:
        raise

    try:
        result = assess_risk(old_data, new_data)
    except Exception:
        # Validation and rule failures are advisory too; never print exception text.
        return FileAnalysis(path, "unavailable", message="Could not analyze: risk assessment could not be completed.")

    findings = tuple(
        (
            change.parameter,
            change.change_type,
            tuple(change.rule_findings),
        )
        for change in result.changes
    )
    if not findings:
        return FileAnalysis(
            path=display_path,
            status="unchanged",
            classification=result.classification,
            risk_score=result.risk_score,
            message="No semantic configuration changes detected.",
        )
    return FileAnalysis(
        path=display_path,
        status="analyzed",
        classification=result.classification,
        risk_score=result.risk_score,
        change_count=len(findings),
        rule_findings=findings,
    )


def _safe_cell(value: str) -> str:
    visible = "".join(character if character.isprintable() else " " for character in value).strip()
    return html.escape(visible, quote=True).replace("`", "&#96;").replace("|", "&#124;")


def _safe_path(path: str) -> str:
    """Keep diagnostic path context while hiding components named like secrets."""
    safe_parts = []
    for part in PurePosixPath(path).parts:
        if _SENSITIVE_PATH_COMPONENT.search(part):
            suffix = PurePosixPath(part).suffix
            safe_parts.append(f"[redacted-name]{suffix}")
        else:
            safe_parts.append(part)
    return _safe_cell("/".join(safe_parts))


def render_summary(results: list[FileAnalysis]) -> str:
    analyzed = sum(result.status == "analyzed" for result in results)
    unchanged = sum(result.status == "unchanged" for result in results)
    unavailable = sum(result.status == "unavailable" for result in results)
    unsupported = sum(result.status == "unsupported" for result in results)
    changes = sum(result.change_count for result in results)
    rules = sum(len(finding[2]) for result in results for finding in result.rule_findings)
    lines = [
        "## ConfigGuard pull request configuration check",
        "",
        "Advisory pairwise change-risk analysis. This check does not use historical baselines or block merges.",
        "",
        f"**Files:** {len(results)} | **Analyzed:** {analyzed} | **Unchanged:** {unchanged} | **Unavailable:** {unavailable} | **Unsupported:** {unsupported} | **Changes:** {changes} | **Rule findings:** {rules}",
        "",
    ]
    if not results:
        lines.extend(["No supported or recognized configuration files changed.", ""])
        return "\n".join(lines)

    if analyzed == 0 and unchanged == 0:
        lines.extend(["No files were analyzed successfully. Review unavailable and unsupported files below.", ""])
    else:
        if rules == 0:
            lines.extend(["No rule-based findings were detected in successfully analyzed files.", ""])
        if unavailable or unsupported:
            lines.extend(["Some changed files could not be analyzed; see the unavailable or unsupported rows below.", ""])

    lines.extend([
        "| File | Result | Classification | Score | Changes |",
        "| --- | --- | ---: | ---: | ---: |",
    ])
    for item in results:
        score = f"{item.risk_score:.2f}" if item.risk_score is not None else "-"
        label = item.message or ("Pairwise change-risk analysis" if item.status == "analyzed" else item.status.title())
        lines.append(
            f"| `{_safe_path(item.path)}` | {_safe_cell(label)} | {_safe_cell(item.classification)} | {score} | {item.change_count} |"
        )
        for parameter, change_type, rule_names in item.rule_findings:
            rule_text = ", ".join(_safe_cell(name) for name in rule_names) if rule_names else "No rule-specific finding; magnitude-based change"
            lines.append(f"| - `{_safe_cell(parameter)}` ({_safe_cell(change_type)}) | {rule_text} |  |  |  |")
    lines.extend(["", "Configuration values and parser source excerpts are intentionally omitted.", ""])
    return "\n".join(lines)


def write_summary(summary: str, destination: str | None = None) -> None:
    target = destination or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as summary_file:
            summary_file.write(summary)
    else:
        sys.stdout.write(summary)


def run(base: str, head: str, repo: str = ".", summary_file: str | None = None) -> list[FileAnalysis]:
    paths = changed_config_paths(repo, base, head)
    results = [
        analyze_changed_file(repo, base, head, item.status, item.path, item.previous_path)
        for item in paths
    ]
    write_summary(render_summary(results), summary_file)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advisory, local ConfigGuard pull request configuration analysis.")
    parser.add_argument("--base", required=True, help="Base commit SHA or ref")
    parser.add_argument("--head", required=True, help="Head commit SHA or ref")
    parser.add_argument("--repo", default=".", help="Git repository path (default: current directory)")
    args = parser.parse_args(argv)
    try:
        run(args.base, args.head, args.repo)
    except OSError:
        # Summary destinations and OS diagnostics can contain runner-specific paths.
        print("Unable to write the analysis summary.", file=sys.stderr)
        return 2
    except GitCommandError as error:
        print(str(error), file=sys.stderr)
        return 2
    except RuntimeError:
        # Do not assume exceptions from future helpers are safe to print.
        print("Unable to complete the configuration analysis.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

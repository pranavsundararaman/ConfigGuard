"""Validated, value-free schemas for GitHub Actions analysis ingestion."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REDACT_LABEL = re.compile(r"(?i)(api[-_]?key|password|token|secret|credential)")
_ALLOWED_RULE_NAMES = {
    "Large retry increase",
    "Large connection pool increase",
    "Significant timeout reduction",
}


def sanitize_ci_name(value: str, *, path: bool = False) -> str:
    """Bound and clean untrusted display names without accepting source text."""
    normalized = value.replace("\\", "/") if path else value
    parts = normalized.split("/") if path else [normalized]
    safe_parts: list[str] = []
    for part in parts:
        if path and part in {"", ".", ".."}:
            continue
        if path and re.fullmatch(r"[A-Za-z]:", part):
            continue
        part = "".join(char for char in part if char.isprintable()).strip()
        if _REDACT_LABEL.search(part):
            suffix = part.rsplit(".", 1)[-1] if "." in part else ""
            part = f"[redacted-name].{suffix}" if suffix else "[redacted-name]"
        safe_parts.append(part)
    result = "/".join(safe_parts).strip("/")
    return result or "[unnamed]"


class CIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CIChangeCounts(CIModel):
    files_analyzed: int = Field(ge=0, le=10000, strict=True)
    files_unchanged: int = Field(ge=0, le=10000, strict=True)
    files_unavailable: int = Field(ge=0, le=10000, strict=True)
    files_unsupported: int = Field(ge=0, le=10000, strict=True)
    configuration_changes: int = Field(ge=0, le=50000, strict=True)
    rule_findings: int = Field(ge=0, le=50000, strict=True)


class CIAnalysisFinding(CIModel):
    file_path: str = Field(min_length=1, max_length=512)
    parameter: str = Field(min_length=1, max_length=512)
    change_type: Literal["ADDED", "REMOVED", "MODIFIED"]
    rule_names: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("file_path", mode="before")
    @classmethod
    def clean_path(cls, value):
        if not isinstance(value, str):
            return value
        return sanitize_ci_name(value, path=True)

    @field_validator("parameter", mode="before")
    @classmethod
    def clean_parameter(cls, value):
        if not isinstance(value, str):
            return value
        return sanitize_ci_name(value)

    @field_validator("rule_names")
    @classmethod
    def allow_known_rules(cls, value: list[str]) -> list[str]:
        if any(name not in _ALLOWED_RULE_NAMES for name in value):
            raise ValueError("Unsupported finding rule.")
        return value


class CIAnalysisIngest(CIModel):
    repository: str = Field(min_length=3, max_length=201)
    pull_request_number: int = Field(ge=1, le=100000000, strict=True)
    pull_request_url: HttpUrl
    base_sha: str = Field(min_length=40, max_length=40)
    head_sha: str = Field(min_length=40, max_length=40)
    risk_score: float = Field(ge=0, le=100, strict=True)
    classification: Literal["SAFE", "REVIEW_REQUIRED", "HIGH_RISK"]
    findings: list[CIAnalysisFinding] = Field(default_factory=list, max_length=500)
    change_counts: CIChangeCounts

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value: str) -> str:
        owner, name = value.split("/", 1) if "/" in value else ("", "")
        if not _REPOSITORY.fullmatch(value) or owner in {".", ".."} or name in {".", ".."}:
            raise ValueError("Invalid repository identifier.")
        return value

    @field_validator("base_sha", "head_sha")
    @classmethod
    def valid_sha(cls, value: str) -> str:
        if not _SHA.fullmatch(value):
            raise ValueError("Invalid commit SHA.")
        return value.lower()

    @model_validator(mode="after")
    def url_matches_repository_and_pr(self):
        parsed = urlsplit(str(self.pull_request_url))
        expected_path = f"/{self.repository}/pull/{self.pull_request_number}"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != expected_path
        ):
            raise ValueError("Pull request URL must match the GitHub repository and number.")
        expected_classification = (
            "HIGH_RISK" if self.risk_score >= 70
            else "REVIEW_REQUIRED" if self.risk_score >= 35
            else "SAFE"
        )
        if self.classification != expected_classification:
            raise ValueError("Risk classification does not match the score.")
        return self


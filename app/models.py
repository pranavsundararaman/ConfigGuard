from typing import Any, Literal
from pydantic import BaseModel, Field

ChangeType = Literal["ADDED", "REMOVED", "MODIFIED"]

class ConfigChange(BaseModel):
    parameter: str
    old_value: Any | None = None
    new_value: Any | None = None
    change_type: ChangeType
    magnitude: float = Field(ge=0, le=100)
    rule_findings: list[str] = Field(default_factory=list)

class AnalysisResponse(BaseModel):
    risk_score: float = Field(ge=0, le=100)
    classification: Literal["SAFE", "REVIEW_REQUIRED", "HIGH_RISK"]
    changes: list[ConfigChange]
    reasons: list[str]

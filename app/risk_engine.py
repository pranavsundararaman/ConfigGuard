from .diff_engine import semantic_diff
from .models import AnalysisResponse
from .rules import evaluate_rules


def assess_risk(old_config: dict, new_config: dict) -> AnalysisResponse:
    changes = semantic_diff(old_config, new_config)

    if not changes:
        return AnalysisResponse(
            risk_score=0.0,
            classification="SAFE",
            changes=[],
            reasons=["No configuration changes detected."]
        )

    reasons: list[str] = []
    max_magnitude = 0.0
    max_rule_score = 0.0

    for change in changes:
        findings, rule_score = evaluate_rules(
            change.parameter,
            change.old_value,
            change.new_value,
        )

        change.rule_findings = findings

        max_magnitude = max(max_magnitude, change.magnitude)
        max_rule_score = max(max_rule_score, rule_score)

        for finding in findings:
            reasons.append(
                f"{change.parameter}: {finding}"
            )


    risk_score = (
        0.60 * max_magnitude
        + 0.40 * max_rule_score
    )

    risk_score = round(min(risk_score, 100.0), 2)

    if risk_score >= 70:
        classification = "HIGH_RISK"
    elif risk_score >= 35:
        classification = "REVIEW_REQUIRED"
    else:
        classification = "SAFE"

    if not reasons:
        reasons.append(
            "Risk is primarily driven by configuration change magnitude."
        )

    return AnalysisResponse(
        risk_score=risk_score,
        classification=classification,
        changes=changes,
        reasons=reasons,
    )
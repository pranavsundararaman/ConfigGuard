from typing import Any

RULES = [
    {
        "name": "Large retry increase",
        "contains": "RETR",
        "condition": lambda old, new: (
            isinstance(old, (int, float)) and isinstance(new, (int, float))
            and old >= 0 and new > old and new / max(old, 1) >= 3
        ),
        "severity": 25,
    },
    {
        "name": "Large connection pool increase",
        "contains": "POOL",
        "condition": lambda old, new: (
            isinstance(old, (int, float)) and isinstance(new, (int, float))
            and old >= 0 and new > old and new / max(old, 1) >= 3
        ),
        "severity": 25,
    },
    {
        "name": "Significant timeout reduction",
        "contains": "TIMEOUT",
        "condition": lambda old, new: (
            isinstance(old, (int, float)) and isinstance(new, (int, float))
            and old > 0 and new < old and new / old <= 0.5
        ),
        "severity": 20,
    },
]

def evaluate_rules(parameter: str, old: Any, new: Any) -> tuple[list[str], float]:
    findings: list[str] = []
    score = 0.0
    upper_parameter = parameter.upper()

    for rule in RULES:
        if rule["contains"] in upper_parameter and rule["condition"](old, new):
            findings.append(rule["name"])
            score += rule["severity"]

    return findings, min(score, 100.0)

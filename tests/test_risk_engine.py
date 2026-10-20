from app.risk_engine import assess_risk

def test_safe_small_change():
    result = assess_risk({"TIMEOUT": 5000}, {"TIMEOUT": 4500})
    assert result.risk_score < 35
    assert result.classification == "SAFE"

def test_retry_change_is_flagged():
    result = assess_risk({"MAX_RETRIES": 3}, {"MAX_RETRIES": 20})
    assert result.classification in {"REVIEW_REQUIRED", "HIGH_RISK"}
    assert any("retry" in reason.lower() for reason in result.reasons)

from app.historical import analyze_historical_values


def test_unusual_numeric_value_reports_transparent_advisory():
    history = [{"limits": {"workers": value}} for value in [4, 4, 5, 4, 5]]
    result = analyze_historical_values({"limits": {"workers": 100}}, history)

    item = result["results"][0]
    assert item["status"] == "unusual"
    assert item["observation_count"] == 5
    assert item["baseline_median"] == 4
    assert "not evidence" in item["explanation"]
    assert result["review_recommended"] is True


def test_insufficient_history_is_not_assessed():
    result = analyze_historical_values({"timeout": 10}, [{"timeout": 5}], min_observations=3)

    assert result["results"][0]["status"] == "insufficient_history"
    assert result["results"][0]["observation_count"] == 1
    assert result["review_recommended"] is False


def test_non_numeric_and_missing_values_are_handled_safely():
    result = analyze_historical_values(
        {"mode": "safe", "timeout": 10},
        [{"timeout": "unknown", "other": 3}] * 4,
    )

    statuses = {item["parameter"]: item["status"] for item in result["results"]}
    assert statuses == {"mode": "not_assessed_non_numeric", "timeout": "insufficient_history"}

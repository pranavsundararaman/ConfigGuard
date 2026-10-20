from app.diff_engine import semantic_diff

def test_detects_modified_parameter():
    changes = semantic_diff({"MAX_RETRIES": 3}, {"MAX_RETRIES": 20})
    assert len(changes) == 1
    assert changes[0].parameter == "MAX_RETRIES"
    assert changes[0].change_type == "MODIFIED"
    assert changes[0].old_value == 3
    assert changes[0].new_value == 20

def test_detects_added_and_removed():
    changes = semantic_diff({"A": 1}, {"B": 2})
    assert {c.change_type for c in changes} == {"ADDED", "REMOVED"}

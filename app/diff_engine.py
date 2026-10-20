from typing import Any
from .models import ConfigChange
from .parser import flatten_config

def _relative_magnitude(old: Any, new: Any) -> float:
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        if old == new:
            return 0.0
        denominator = max(abs(float(old)), 1e-9)
        return min(abs(float(new) - float(old)) / denominator * 100.0, 100.0)
    return 100.0 if old != new else 0.0

def semantic_diff(old_config: dict[str, Any], new_config: dict[str, Any]) -> list[ConfigChange]:
    old_flat = flatten_config(old_config)
    new_flat = flatten_config(new_config)
    changes: list[ConfigChange] = []

    for parameter in sorted(set(old_flat) | set(new_flat)):
        in_old = parameter in old_flat
        in_new = parameter in new_flat

        if not in_old:
            changes.append(ConfigChange(
                parameter=parameter, new_value=new_flat[parameter],
                change_type="ADDED", magnitude=100.0
            ))
        elif not in_new:
            changes.append(ConfigChange(
                parameter=parameter, old_value=old_flat[parameter],
                change_type="REMOVED", magnitude=100.0
            ))
        elif old_flat[parameter] != new_flat[parameter]:
            changes.append(ConfigChange(
                parameter=parameter,
                old_value=old_flat[parameter],
                new_value=new_flat[parameter],
                change_type="MODIFIED",
                magnitude=_relative_magnitude(old_flat[parameter], new_flat[parameter])
            ))

    return changes

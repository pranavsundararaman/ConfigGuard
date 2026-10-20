import math
from statistics import median
from typing import Any

from .parser import flatten_config

MIN_HISTORICAL_OBSERVATIONS = 3


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def analyze_historical_values(
    proposed: dict[str, Any],
    prior_configurations: list[dict[str, Any]],
    min_observations: int = MIN_HISTORICAL_OBSERVATIONS,
) -> dict[str, Any]:
    """Compare proposed numeric values with per-parameter historical baselines.

    A robust median/MAD outlier check is advisory; it does not predict deployment
    failure. Each parameter is evaluated only when enough numeric observations
    for that parameter are available.
    """
    if (
        isinstance(min_observations, bool)
        or not isinstance(min_observations, int)
        or min_observations < MIN_HISTORICAL_OBSERVATIONS
    ):
        raise ValueError(
            f"min_observations must be at least {MIN_HISTORICAL_OBSERVATIONS}."
        )

    proposed_flat = flatten_config(proposed)
    prior_flat = [flatten_config(config) for config in prior_configurations]
    results: list[dict[str, Any]] = []

    for parameter, proposed_value in proposed_flat.items():
        if not _finite_number(proposed_value):
            results.append({
                "parameter": parameter,
                "status": "not_assessed_non_numeric",
                "proposed_value": proposed_value,
                "observation_count": 0,
                "explanation": "Historical numeric analysis is not applicable to this non-numeric value.",
            })
            continue

        observations = [
            float(config[parameter])
            for config in prior_flat
            if parameter in config and _finite_number(config[parameter])
        ]
        if len(observations) < min_observations:
            results.append({
                "parameter": parameter,
                "status": "insufficient_history",
                "proposed_value": proposed_value,
                "observation_count": len(observations),
                "required_observations": min_observations,
                "explanation": (
                    f"Only {len(observations)} numeric prior observation(s) are available; "
                    f"at least {min_observations} are required. No historical judgment was made."
                ),
            })
            continue

        center = median(observations)
        deviations = [abs(value - center) for value in observations]
        mad = median(deviations)
        distance = abs(float(proposed_value) - center)
        robust_z = 0.6745 * distance / mad if mad else (0.0 if distance == 0 else math.inf)
        unusual = robust_z >= 3.5
        results.append({
            "parameter": parameter,
            "status": "unusual" if unusual else "within_historical_range",
            "proposed_value": proposed_value,
            "observation_count": len(observations),
            "baseline_median": center,
            "baseline_mad": mad,
            "robust_deviation": None if math.isinf(robust_z) else round(robust_z, 3),
            "explanation": (
                f"The proposed value is unusual relative to {len(observations)} prior numeric "
                f"observation(s) (median {center:g}, MAD {mad:g}). This is a review signal, "
                "not evidence that deployment will fail."
                if unusual else
                f"The proposed value is not an outlier relative to {len(observations)} prior "
                f"numeric observation(s) (median {center:g}, MAD {mad:g}); this does not "
                "guarantee deployment success."
            ),
        })

    return {
        "method": "per-parameter median and median absolute deviation (MAD); robust deviation threshold 3.5",
        "minimum_observations": min_observations,
        "results": results,
        "review_recommended": any(item["status"] == "unusual" for item in results),
        "limitations": "Historical unusualness is an advisory review signal and does not predict deployment failure.",
    }

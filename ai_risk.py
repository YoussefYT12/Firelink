"""
ai_risk.py (the fully local version)
------------------------------------
This is the "AI" layer in FireLink, but it currently runs entirely inside
the project - with no connection to any external API (no Anthropic or others), and
without requiring internet at all right now. Everything runs locally on your machine.

Why is it called "AI" even though it's not an LLM? Because it's a **Rule-Based Expert
System** - a classic, long-standing branch of artificial intelligence
(even before neural networks), and it simulates how a real expert would think if
they saw the same numbers. The difference is that its decisions are 100% fixed and
clear (Transparent), not a "black box" like an LLM - and that's an actual advantage
in an alerting system built on important decisions.

If you later want to connect it to a real LLM (e.g. Claude) as an additional layer on top,
the code is designed to make replacement easy - but it's not required for the project to run.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskAssessment:
    risk_level: str
    summary: str
    reasoning: str
    recommended_action: str


# --- Decision thresholds (Thresholds) - Easily adjustable based on your experience ---
FRP_HIGH = 100.0      # Fire Radiative Power high = intense/high-heat fire
FRP_MEDIUM = 40.0
CONFIRMED_HIGH = 10   # The number of "confirmed by two sources" fires is high

# Threshold to distinguish between a likely agricultural fire and a likely forest/wildland fire, based on
# Heat intensity only (FRP) - we don't have actual Land Cover data (Land Cover),
# So this is a professional estimate, not a 100% certain classification. Agricultural fires (burning of crop
# residues) are usually weaker and shorter than actual forest fires.
AGRI_FRP_THRESHOLD = 50.0


def classify_fire_type(avg_frp: float, max_frp: float) -> str:
    """    Estimated classification: 'Likely agricultural/crop burning' or 'Likely wildfire/forest fire'.
    Based only on heat intensity, so it's a probabilistic indicator not a confirmation - field verification
    or a satellite image is recommended if the decision is sensitive.
    """
    if max_frp < AGRI_FRP_THRESHOLD:
        return "Likely agricultural/crop burning"
    return "Likely wildfire/forest fire"
CONFIRMED_MEDIUM = 3
GROWTH_CRITICAL = 5   # Sudden increase in the number of confirmed fires between two cycles

_LEVEL_ORDER = ["Low", "Medium", "High", "Critical"]


def _max_level(a: str, b: str) -> str:
    """Returns the highest severity level between two."""
    return a if _LEVEL_ORDER.index(a) >= _LEVEL_ORDER.index(b) else b


def _summary_for_level(level: str, confirmed: int, likely: int) -> str:
    templates = {
        "Low": f"No significant fire activity detected ({confirmed} confirmed, {likely} likely).",
        "Medium": f"Moderate fire activity detected: {confirmed} confirmed hotspots in the area.",
        "High": f"Significant fire activity detected: {confirmed} confirmed hotspots, worth close attention.",
        "Critical": f"Rapidly escalating fire activity: {confirmed} confirmed hotspots and rising fast.",
    }
    return templates[level]


def _action_for_level(level: str) -> str:
    actions = {
        "Low": "Continue routine monitoring at the normal polling interval.",
        "Medium": "Increase monitoring frequency and flag the area for a human review.",
        "High": "Notify local emergency contacts and monitor at shorter intervals.",
        "Critical": "Escalate immediately to emergency responders - this pattern warrants urgent attention.",
    }
    return actions[level]


def assess_risk(stats: dict) -> RiskAssessment:
    """
    Simple expert system: takes statistics (from build_stats_from_results) and applies
    a clear set of rules to arrive at a risk assessment + explanation + recommendation.
    """
    confirmed = stats.get("confirmed_count", 0)
    likely = stats.get("likely_count", 0)
    avg_frp = stats.get("average_frp", 0.0)
    max_frp = stats.get("max_frp", 0.0)
    growth = stats.get("confirmed_growth")  # May be None on first reading

    reasons = []
    level = "Low"

    if growth is not None and growth >= GROWTH_CRITICAL:
        level = "Critical"
        reasons.append(
            f"confirmed fires jumped by {growth} since the last check - rapid escalation"
        )
    elif growth is not None and growth >= 2:
        level = _max_level(level, "High")
        reasons.append(f"confirmed fires increased by {growth} since the last check")

    if confirmed >= CONFIRMED_HIGH:
        level = _max_level(level, "High")
        reasons.append(f"{confirmed} fires confirmed by both satellites simultaneously")
    elif confirmed >= CONFIRMED_MEDIUM:
        level = _max_level(level, "Medium")
        reasons.append(f"{confirmed} fires confirmed by both satellites")

    if max_frp >= FRP_HIGH:
        level = _max_level(level, "High")
        reasons.append(f"peak fire radiative power ({max_frp} MW) indicates an intense fire")
    elif avg_frp >= FRP_MEDIUM:
        level = _max_level(level, "Medium")
        reasons.append(f"average fire radiative power ({avg_frp} MW) is elevated")

    if not reasons:
        reasons.append("detection counts and intensity are within normal background levels")

    summary = _summary_for_level(level, confirmed, likely)
    reasoning = "; ".join(reasons) + "."
    action = _action_for_level(level)

    return RiskAssessment(
        risk_level=level,
        summary=summary,
        reasoning=reasoning,
        recommended_action=action,
    )


def build_stats_from_results(results, previous_confirmed_count: Optional[int] = None) -> dict:
    """
    Helper function that builds the statistics required for assess_risk() directly from
    the result of reconcile() returned by reconciliation.py.
    """
    confirmed = [r for r in results if r.status.name == "CONFIRMED"]
    likely = [r for r in results if r.status.name == "LIKELY"]

    frps = []
    for r in results:
        for det in (r.viirs_detection, r.modis_detection):
            if det is not None and det.frp:
                frps.append(det.frp)

    avg_frp = round(sum(frps) / len(frps), 2) if frps else 0.0
    max_frp = round(max(frps), 2) if frps else 0.0

    stats = {
        "confirmed_count": len(confirmed),
        "likely_count": len(likely),
        "total_detections": len(results),
        "average_frp": avg_frp,
        "max_frp": max_frp,
    }

    if previous_confirmed_count is not None:
        stats["previous_confirmed_count"] = previous_confirmed_count
        stats["confirmed_growth"] = len(confirmed) - previous_confirmed_count

    return stats


if __name__ == "__main__":
    demo_stats = {
        "confirmed_count": 14,
        "likely_count": 22,
        "total_detections": 210,
        "average_frp": 45.2,
        "max_frp": 210.8,
        "previous_confirmed_count": 6,
        "confirmed_growth": 8,
    }
    result = assess_risk(demo_stats)
    print(f"Risk Level: {result.risk_level}")
    print(f"Summary:    {result.summary}")
    print(f"Reasoning:  {result.reasoning}")
    print(f"Action:     {result.recommended_action}")
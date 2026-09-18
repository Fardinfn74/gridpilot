"""
guardrails.py — Deterministic validation and merge layer for GridPilot AI.

Takes raw LLM-returned directive entries, enforces the rules in Section 5
of the problem statement, and returns cleaned, merged entries ready for
the optimizer. Never throws on bad LLM output — degrades to no_op per note.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from app.schemas import BatterySpec

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


def _safe_no_op(note_index: int, reason: str, original_explanation: str = "") -> Dict[str, Any]:
    """Return a safe no_op entry with a logged warning."""
    logger.warning(
        "Guardrail: note_index=%d forced to no_op — %s", note_index, reason
    )
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": original_explanation or f"Guardrail fallback: {reason}",
    }


def _validate_hours_list(hours_val: Any) -> Optional[List[int]]:
    """
    Validate and normalise an 'hours' field.
    Returns sorted unique list of valid hours, or None if invalid.
    """
    if not isinstance(hours_val, list):
        return None
    ints = []
    for v in hours_val:
        if not isinstance(v, (int, float)) or math.isnan(float(v)):
            return None
        iv = int(v)
        if iv < 0 or iv > 23:
            return None
        ints.append(iv)
    if len(ints) == 0:
        return None
    unique_sorted = sorted(set(ints))
    return unique_sorted


def validate_and_clean(
    raw_entries: List[Dict[str, Any]],
    num_notes: int,
    battery: BatterySpec,
) -> List[Dict[str, Any]]:
    """
    Validates and cleans LLM-returned entries per the guardrail rules.

    1. Fills missing indices with no_op.
    2. Forces directive_type to no_op if unknown.
    3. Validates structured_adjustment fields per directive type.
    4. Returns exactly num_notes entries, indexed 0..num_notes-1.
    """
    # Index existing entries by note_index
    indexed: Dict[int, Dict[str, Any]] = {}
    for entry in raw_entries:
        try:
            ni = int(entry.get("note_index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= ni < num_notes:
            if ni not in indexed:
                indexed[ni] = entry  # first occurrence wins

    cleaned: List[Dict[str, Any]] = []

    for ni in range(num_notes):
        if ni not in indexed:
            cleaned.append(_safe_no_op(ni, "missing note_index from LLM"))
            continue

        entry = indexed[ni]
        dtype = entry.get("directive_type", "")
        explanation = str(entry.get("explanation", ""))

        if dtype not in ALLOWED_TYPES:
            cleaned.append(_safe_no_op(ni, f"unknown directive_type '{dtype}'", explanation))
            continue

        if dtype == "no_op":
            cleaned.append({
                "note_index": ni,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": explanation,
            })
            continue

        # For non-no_op, applies must be True
        adj = entry.get("structured_adjustment") or {}

        if not isinstance(adj, dict):
            cleaned.append(_safe_no_op(ni, "structured_adjustment is not an object", explanation))
            continue

        hours = _validate_hours_list(adj.get("hours"))
        if hours is None:
            cleaned.append(_safe_no_op(ni, "invalid hours array", explanation))
            continue

        # Type-specific validation
        if dtype == "solar_reduction":
            factor = adj.get("factor")
            try:
                factor = float(factor)
                assert 0.0 <= factor <= 1.0
            except (TypeError, ValueError, AssertionError):
                cleaned.append(_safe_no_op(ni, f"invalid solar_reduction factor={factor}", explanation))
                continue
            cleaned.append({
                "note_index": ni,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": hours, "factor": round(factor, 6)},
                "explanation": explanation,
            })

        elif dtype == "minimum_battery_reserve":
            min_kwh = adj.get("minimum_energy_kwh")
            try:
                min_kwh = float(min_kwh)
                assert math.isfinite(min_kwh) and min_kwh >= 0.0 and min_kwh <= battery.capacity_kwh
            except (TypeError, ValueError, AssertionError):
                cleaned.append(_safe_no_op(ni, f"invalid minimum_energy_kwh={min_kwh}", explanation))
                continue
            cleaned.append({
                "note_index": ni,
                "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": hours, "minimum_energy_kwh": min_kwh},
                "explanation": explanation,
            })

        elif dtype in ("no_charge_window", "no_discharge_window"):
            cleaned.append({
                "note_index": ni,
                "applies": True,
                "directive_type": dtype,
                "structured_adjustment": {"hours": hours},
                "explanation": explanation,
            })

        elif dtype == "max_grid_window":
            max_g = adj.get("max_grid_kwh")
            try:
                max_g = float(max_g)
                assert math.isfinite(max_g) and max_g >= 0.0
            except (TypeError, ValueError, AssertionError):
                cleaned.append(_safe_no_op(ni, f"invalid max_grid_kwh={max_g}", explanation))
                continue
            cleaned.append({
                "note_index": ni,
                "applies": True,
                "directive_type": "max_grid_window",
                "structured_adjustment": {"hours": hours, "max_grid_kwh": max_g},
                "explanation": explanation,
            })

        else:
            # Shouldn't reach here, but safety net
            cleaned.append(_safe_no_op(ni, f"unhandled dtype={dtype}", explanation))

    return cleaned

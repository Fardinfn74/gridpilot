"""
validator.py — Final plan replay validator for GridPilot AI.

Replays the optimizer output hour-by-hour to verify:
  - Energy balance (grid + solar + discharge == demand + charge)
  - Battery state bounds
  - End-of-day neutrality
  - Recomputes total_grid_kwh, total_cost_bdt, peak_grid_kwh from hourly_plan

Raises ValidationError if any constraint is violated.
"""
from __future__ import annotations

import logging
import math
from typing import List

from app.schemas import BatterySpec, HourData, HourlyPlanEntry

logger = logging.getLogger(__name__)

TOLERANCE = 0.05  # kWh tolerance for replay check (slightly relaxed for floating point)


class PlanValidationError(Exception):
    """Raised when the final plan fails replay validation."""


def recompute_totals(
    hourly_plan: List[HourlyPlanEntry],
    hours_data: List[HourData],
) -> tuple[float, float, float]:
    """
    Recompute total_grid_kwh, total_cost_bdt, peak_grid_kwh directly
    from hourly_plan. These MUST NOT be taken from optimizer internals.
    Returns (total_grid_kwh, total_cost_bdt, peak_grid_kwh).
    """
    tariff = {h.hour: h.tariff_bdt_per_kwh for h in hours_data}

    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for entry in hourly_plan:
        g = entry.grid_kwh
        total_grid += g
        total_cost += g * tariff[entry.hour]
        if g > peak_grid:
            peak_grid = g

    return round(total_grid, 4), round(total_cost, 4), round(peak_grid, 4)


def validate_plan(
    hourly_plan: List[HourlyPlanEntry],
    hours_data: List[HourData],
    battery: BatterySpec,
) -> None:
    """
    Replay the 24-hour plan and verify all physical constraints hold.
    Raises PlanValidationError describing the first violation found.
    """
    demand = {h.hour: h.demand_kwh for h in hours_data}
    bat_energy = battery.initial_energy_kwh
    plan_sorted = sorted(hourly_plan, key=lambda e: e.hour)

    for entry in plan_sorted:
        h = entry.hour
        dem = demand[h]
        g = entry.grid_kwh
        s = entry.solar_used_kwh
        bk = entry.battery_kwh
        ba = entry.battery_action
        ba_after = entry.battery_energy_after_kwh

        # Derive charge and discharge from action
        charge_kwh = bk if ba == "charge" else 0.0
        discharge_kwh = bk if ba == "discharge" else 0.0

        # Energy balance
        lhs = g + s + discharge_kwh
        rhs = dem + charge_kwh
        if abs(lhs - rhs) > TOLERANCE:
            raise PlanValidationError(
                f"Hour {h}: Energy balance violated. "
                f"grid({g}) + solar({s}) + discharge({discharge_kwh}) = {lhs:.4f} "
                f"!= demand({dem}) + charge({charge_kwh}) = {rhs:.4f}"
            )

        # Battery state
        expected_bat = bat_energy + charge_kwh - discharge_kwh
        if abs(expected_bat - ba_after) > TOLERANCE:
            raise PlanValidationError(
                f"Hour {h}: Battery energy state mismatch. "
                f"Expected {expected_bat:.4f}, got {ba_after:.4f}"
            )

        # Battery bounds
        if ba_after < battery.minimum_energy_kwh - TOLERANCE:
            raise PlanValidationError(
                f"Hour {h}: Battery fell below minimum reserve. "
                f"{ba_after:.4f} < {battery.minimum_energy_kwh:.4f}"
            )
        if ba_after > battery.capacity_kwh + TOLERANCE:
            raise PlanValidationError(
                f"Hour {h}: Battery exceeded capacity. "
                f"{ba_after:.4f} > {battery.capacity_kwh:.4f}"
            )

        # Non-negative
        if g < -TOLERANCE or s < -TOLERANCE or bk < -TOLERANCE:
            raise PlanValidationError(
                f"Hour {h}: Negative value in plan (grid={g}, solar={s}, bk={bk})"
            )

        bat_energy = ba_after

    # End-of-day neutrality
    if abs(bat_energy - battery.initial_energy_kwh) > TOLERANCE:
        raise PlanValidationError(
            f"End-of-day battery neutrality violated: "
            f"final={bat_energy:.4f} != initial={battery.initial_energy_kwh:.4f}"
        )

    logger.info("Plan replay validation passed.")

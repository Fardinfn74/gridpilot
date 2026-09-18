"""
optimizer.py — PuLP MILP energy optimizer for GridPilot AI.

Minimizes total grid cost subject to:
  - Energy balance every hour
  - Battery charge/discharge bounds and mutual exclusion
  - Battery energy state bounds (including directive minimum reserves)
  - End-of-day battery neutrality (final energy == initial energy)
  - Directive constraints (solar reduction, no_charge/discharge windows, max_grid)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pulp

from app.schemas import BatterySpec, HourData, HourlyPlanEntry

logger = logging.getLogger(__name__)

TOLERANCE = 0.01  # kWh / BDT tolerance per problem statement


@dataclass
class DirectiveConstraints:
    """Aggregated directive effects to be applied to the MILP model."""
    # Per-hour effective solar (after solar_reduction). Key = hour (0-23).
    effective_solar: Dict[int, float] = field(default_factory=dict)
    # Per-hour minimum battery reserve (max of base and any directive min). Key = hour.
    min_battery_reserve: Dict[int, float] = field(default_factory=dict)
    # Set of hours where charging is forbidden.
    no_charge_hours: set = field(default_factory=set)
    # Set of hours where discharging is forbidden.
    no_discharge_hours: set = field(default_factory=set)
    # Per-hour max grid draw. Key = hour.
    max_grid: Dict[int, float] = field(default_factory=dict)


def build_directive_constraints(
    hours_data: List[HourData],
    battery: BatterySpec,
    validated_directives: list,
) -> DirectiveConstraints:
    """
    Convert a list of guardrail-validated directive_interpretation entries
    into DirectiveConstraints ready to pass to solve_milp().
    Merging rules (as per Section 1.4) are applied here.
    """
    dc = DirectiveConstraints()

    # Initialise effective solar from input
    for h in hours_data:
        dc.effective_solar[h.hour] = h.solar_kwh
        dc.min_battery_reserve[h.hour] = battery.minimum_energy_kwh

    for entry in validated_directives:
        if not entry.get("applies", False):
            continue
        dtype = entry.get("directive_type")
        adj = entry.get("structured_adjustment") or {}
        entry_hours = adj.get("hours", [])

        if dtype == "solar_reduction":
            factor = float(adj.get("factor", 1.0))
            for h in entry_hours:
                # Merge: multiply factors (most restrictive)
                dc.effective_solar[h] = dc.effective_solar.get(h, hours_data[h].solar_kwh) * factor

        elif dtype == "minimum_battery_reserve":
            min_kwh = float(adj.get("minimum_energy_kwh", 0.0))
            for h in entry_hours:
                # Merge: take maximum (most restrictive)
                dc.min_battery_reserve[h] = max(dc.min_battery_reserve.get(h, battery.minimum_energy_kwh), min_kwh)

        elif dtype == "no_charge_window":
            for h in entry_hours:
                dc.no_charge_hours.add(h)

        elif dtype == "no_discharge_window":
            for h in entry_hours:
                dc.no_discharge_hours.add(h)

        elif dtype == "max_grid_window":
            max_g = float(adj.get("max_grid_kwh", 1e9))
            for h in entry_hours:
                # Merge: take minimum (most restrictive)
                dc.max_grid[h] = min(dc.max_grid.get(h, 1e9), max_g)

    return dc


def solve_milp(
    hours_data: List[HourData],
    battery: BatterySpec,
    dc: DirectiveConstraints,
) -> List[HourlyPlanEntry]:
    """
    Build and solve the 24-hour energy MILP.
    Returns a list of HourlyPlanEntry (sorted hour 0-23).
    Raises RuntimeError if the problem is infeasible or unbounded.
    """
    prob = pulp.LpProblem("GridPilot_Energy", pulp.LpMinimize)

    hours = list(range(24))
    cap = battery.capacity_kwh
    max_c = battery.max_charge_kwh_per_hour
    max_d = battery.max_discharge_kwh_per_hour
    init_e = battery.initial_energy_kwh

    # ----- Decision variables -----
    grid = {h: pulp.LpVariable(f"grid_{h}", lowBound=0) for h in hours}
    solar_used = {h: pulp.LpVariable(f"solar_used_{h}", lowBound=0) for h in hours}
    charge = {h: pulp.LpVariable(f"charge_{h}", lowBound=0, upBound=max_c) for h in hours}
    discharge = {h: pulp.LpVariable(f"discharge_{h}", lowBound=0, upBound=max_d) for h in hours}
    bat_after = {h: pulp.LpVariable(f"bat_after_{h}", lowBound=0, upBound=cap) for h in hours}
    # Binary: 1 = charging this hour, 0 = discharging or idle
    is_charging = {h: pulp.LpVariable(f"is_charging_{h}", cat="Binary") for h in hours}

    # ----- Objective -----
    tariff = {h.hour: h.tariff_bdt_per_kwh for h in hours_data}
    demand = {h.hour: h.demand_kwh for h in hours_data}

    prob += pulp.lpSum(grid[h] * tariff[h] for h in hours), "minimize_grid_cost"

    # ----- Constraints -----
    for h in hours:
        eff_solar = dc.effective_solar.get(h, hours_data[h].solar_kwh)
        dem = demand[h]
        bat_prev = init_e if h == 0 else bat_after[h - 1]
        base_min_reserve = dc.min_battery_reserve.get(h, battery.minimum_energy_kwh)

        # Energy balance: grid + solar_used + discharge == demand + charge
        prob += (
            grid[h] + solar_used[h] + discharge[h] == dem + charge[h],
            f"energy_balance_{h}",
        )

        # Solar curtailment (no export): solar_used <= effective_solar
        prob += solar_used[h] <= eff_solar, f"solar_cap_{h}"

        # Battery state evolution
        prob += bat_after[h] == bat_prev + charge[h] - discharge[h], f"bat_state_{h}"

        # Battery reserve bounds
        prob += bat_after[h] >= base_min_reserve, f"bat_min_{h}"
        prob += bat_after[h] <= cap, f"bat_max_{h}"

        # Mutual exclusion: charge and discharge cannot happen in same hour
        prob += charge[h] <= max_c * is_charging[h], f"charge_binary_{h}"
        prob += discharge[h] <= max_d * (1 - is_charging[h]), f"discharge_binary_{h}"

        # Directive: no_charge_window
        if h in dc.no_charge_hours:
            prob += charge[h] == 0, f"no_charge_{h}"

        # Directive: no_discharge_window
        if h in dc.no_discharge_hours:
            prob += discharge[h] == 0, f"no_discharge_{h}"

        # Directive: max_grid_window
        if h in dc.max_grid:
            prob += grid[h] <= dc.max_grid[h], f"max_grid_{h}"

    # End-of-day neutrality: battery_after[23] == initial_energy_kwh
    prob += bat_after[23] == init_e, "end_of_day_neutrality"

    # ----- Solve -----
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=20)
    status = prob.solve(solver)

    if pulp.LpStatus[status] not in ("Optimal",):
        raise RuntimeError(
            f"MILP solver returned non-optimal status: {pulp.LpStatus[status]}"
        )

    # ----- Extract hourly plan -----
    plan: List[HourlyPlanEntry] = []
    for h in hours:
        c_val = max(0.0, pulp.value(charge[h]) or 0.0)
        d_val = max(0.0, pulp.value(discharge[h]) or 0.0)
        g_val = max(0.0, pulp.value(grid[h]) or 0.0)
        s_val = max(0.0, pulp.value(solar_used[h]) or 0.0)
        ba_val = pulp.value(bat_after[h]) or 0.0

        # Derive battery_action and battery_kwh
        if c_val > TOLERANCE:
            bat_action = "charge"
            bat_kwh = round(c_val, 4)
        elif d_val > TOLERANCE:
            bat_action = "discharge"
            bat_kwh = round(d_val, 4)
        else:
            bat_action = "idle"
            bat_kwh = 0.0

        plan.append(
            HourlyPlanEntry(
                hour=h,
                grid_kwh=round(g_val, 4),
                solar_used_kwh=round(s_val, 4),
                battery_action=bat_action,
                battery_kwh=bat_kwh,
                battery_energy_after_kwh=round(ba_val, 4),
            )
        )

    return plan

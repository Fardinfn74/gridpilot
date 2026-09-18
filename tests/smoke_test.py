"""Quick optimizer standalone smoke test."""
from app.schemas import HourData, BatterySpec
from app.optimizer import solve_milp, DirectiveConstraints
from app.validator import validate_plan, recompute_totals

hours_data = [
    HourData(hour=0,  demand_kwh=40, solar_kwh=0,  tariff_bdt_per_kwh=6.0),
    HourData(hour=1,  demand_kwh=38, solar_kwh=0,  tariff_bdt_per_kwh=6.0),
    HourData(hour=2,  demand_kwh=35, solar_kwh=0,  tariff_bdt_per_kwh=6.0),
    HourData(hour=3,  demand_kwh=33, solar_kwh=0,  tariff_bdt_per_kwh=6.0),
    HourData(hour=4,  demand_kwh=32, solar_kwh=0,  tariff_bdt_per_kwh=6.0),
    HourData(hour=5,  demand_kwh=34, solar_kwh=0,  tariff_bdt_per_kwh=6.0),
    HourData(hour=6,  demand_kwh=40, solar_kwh=5,  tariff_bdt_per_kwh=7.0),
    HourData(hour=7,  demand_kwh=55, solar_kwh=15, tariff_bdt_per_kwh=7.0),
    HourData(hour=8,  demand_kwh=70, solar_kwh=30, tariff_bdt_per_kwh=8.0),
    HourData(hour=9,  demand_kwh=80, solar_kwh=50, tariff_bdt_per_kwh=8.0),
    HourData(hour=10, demand_kwh=85, solar_kwh=65, tariff_bdt_per_kwh=9.0),
    HourData(hour=11, demand_kwh=90, solar_kwh=75, tariff_bdt_per_kwh=9.0),
    HourData(hour=12, demand_kwh=95, solar_kwh=80, tariff_bdt_per_kwh=10.0),
    HourData(hour=13, demand_kwh=95, solar_kwh=15, tariff_bdt_per_kwh=10.0),
    HourData(hour=14, demand_kwh=90, solar_kwh=13, tariff_bdt_per_kwh=10.0),
    HourData(hour=15, demand_kwh=85, solar_kwh=55, tariff_bdt_per_kwh=9.0),
    HourData(hour=16, demand_kwh=80, solar_kwh=40, tariff_bdt_per_kwh=9.0),
    HourData(hour=17, demand_kwh=85, solar_kwh=20, tariff_bdt_per_kwh=9.0),
    HourData(hour=18, demand_kwh=95, solar_kwh=5,  tariff_bdt_per_kwh=11.0),
    HourData(hour=19, demand_kwh=100,solar_kwh=0,  tariff_bdt_per_kwh=11.0),
    HourData(hour=20, demand_kwh=95, solar_kwh=0,  tariff_bdt_per_kwh=10.0),
    HourData(hour=21, demand_kwh=80, solar_kwh=0,  tariff_bdt_per_kwh=9.0),
    HourData(hour=22, demand_kwh=60, solar_kwh=0,  tariff_bdt_per_kwh=7.0),
    HourData(hour=23, demand_kwh=45, solar_kwh=0,  tariff_bdt_per_kwh=6.0),
]
battery = BatterySpec(
    capacity_kwh=200,
    initial_energy_kwh=100,
    minimum_energy_kwh=20,
    max_charge_kwh_per_hour=50,
    max_discharge_kwh_per_hour=50,
)

dc = DirectiveConstraints()
for h in hours_data:
    dc.effective_solar[h.hour] = h.solar_kwh
    dc.min_battery_reserve[h.hour] = battery.minimum_energy_kwh

plan = solve_milp(hours_data, battery, dc)
assert len(plan) == 24, f"Expected 24 entries, got {len(plan)}"

# Replay validate
validate_plan(plan, hours_data, battery)

total_grid, total_cost, peak = recompute_totals(plan, hours_data)
print(f"total_grid_kwh={total_grid:.2f}, total_cost_bdt={total_cost:.2f}, peak_grid_kwh={peak:.2f}")
print(f"End battery: {plan[23].battery_energy_after_kwh:.4f} (initial: {battery.initial_energy_kwh})")
print("OPTIMIZER SMOKE TEST PASSED")

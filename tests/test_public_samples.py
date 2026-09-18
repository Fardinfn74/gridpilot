"""
test_public_samples.py — Run all public sample cases against the running service.

Usage:
  # Start the service first:
  uvicorn app.main:app --host 0.0.0.0 --port 8000

  # Then run:
  python tests/test_public_samples.py [--url http://localhost:8000] [--file tests/public_samples.json]

Reports pass/fail for:
  - Schema validity (scenario_id echo, 24 hourly_plan entries, 1 directive per note)
  - Energy balance per hour
  - Battery bound compliance per hour
  - End-of-day neutrality
  - applies=False only for no_op
  - total_grid_kwh / total_cost_bdt / peak_grid_kwh consistency with hourly_plan
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx

TOLERANCE = 0.05  # kWh/BDT replay tolerance


def check_case(case: Dict[str, Any], base_url: str) -> List[str]:
    """Send one case to the service and return a list of failure messages (empty = pass)."""
    failures: List[str] = []
    sid = case.get("scenario_id", "?")

    try:
        resp = httpx.post(
            f"{base_url}/optimize-energy",
            json=case,
            timeout=35.0,
        )
    except Exception as exc:
        return [f"HTTP error: {exc}"]

    if resp.status_code != 200:
        return [f"HTTP {resp.status_code}: {resp.text[:200]}"]

    try:
        data = resp.json()
    except Exception:
        return ["Response is not valid JSON"]

    # 1. scenario_id echo
    if data.get("scenario_id") != sid:
        failures.append(f"scenario_id mismatch: got {data.get('scenario_id')!r}")

    # 2. directive_interpretation count
    di = data.get("directive_interpretation", [])
    n_notes = len(case.get("operator_notes", []))
    if len(di) != n_notes:
        failures.append(f"Expected {n_notes} directive entries, got {len(di)}")
    else:
        for i, entry in enumerate(di):
            if entry.get("note_index") != i:
                failures.append(f"directive_interpretation[{i}].note_index = {entry.get('note_index')!r} (expected {i})")
            if entry.get("directive_type") == "no_op" and entry.get("applies") is True:
                failures.append(f"directive_interpretation[{i}]: no_op must have applies=false")
            if entry.get("directive_type") != "no_op" and entry.get("applies") is False:
                failures.append(f"directive_interpretation[{i}]: non-no_op must have applies=true")

    # 3. hourly_plan
    hp = data.get("hourly_plan", [])
    if len(hp) != 24:
        failures.append(f"Expected 24 hourly_plan entries, got {len(hp)}")
        return failures  # Cannot validate further

    hp_sorted = sorted(hp, key=lambda e: e.get("hour", -1))
    hours_by_hour = {h["hour"]: h for h in case.get("hours", [])}
    battery = case.get("battery", {})
    cap = battery.get("capacity_kwh", float("inf"))
    min_e = battery.get("minimum_energy_kwh", 0.0)
    init_e = battery.get("initial_energy_kwh", 0.0)
    max_c = battery.get("max_charge_kwh_per_hour", float("inf"))
    max_d = battery.get("max_discharge_kwh_per_hour", float("inf"))

    bat_energy = init_e
    for entry in hp_sorted:
        h = entry.get("hour")
        demand = hours_by_hour.get(h, {}).get("demand_kwh", 0.0)
        tariff = hours_by_hour.get(h, {}).get("tariff_bdt_per_kwh", 0.0)

        g = entry.get("grid_kwh", 0.0)
        s = entry.get("solar_used_kwh", 0.0)
        bk = entry.get("battery_kwh", 0.0)
        ba = entry.get("battery_action", "idle")
        ba_after = entry.get("battery_energy_after_kwh", 0.0)

        charge_kwh = bk if ba == "charge" else 0.0
        discharge_kwh = bk if ba == "discharge" else 0.0

        # Energy balance
        lhs = g + s + discharge_kwh
        rhs = demand + charge_kwh
        if abs(lhs - rhs) > TOLERANCE:
            failures.append(
                f"Hour {h}: energy balance violated ({lhs:.4f} != {rhs:.4f})"
            )

        # Battery state
        expected = bat_energy + charge_kwh - discharge_kwh
        if abs(expected - ba_after) > TOLERANCE:
            failures.append(f"Hour {h}: battery state mismatch ({expected:.4f} != {ba_after:.4f})")

        # Battery bounds
        if ba_after < min_e - TOLERANCE:
            failures.append(f"Hour {h}: battery below min reserve ({ba_after:.4f} < {min_e:.4f})")
        if ba_after > cap + TOLERANCE:
            failures.append(f"Hour {h}: battery exceeds capacity ({ba_after:.4f} > {cap:.4f})")

        bat_energy = ba_after

    # End-of-day neutrality
    if abs(bat_energy - init_e) > TOLERANCE:
        failures.append(f"End-of-day neutrality: final={bat_energy:.4f} != initial={init_e:.4f}")

    # 4. Totals consistency
    total_grid_calc = sum(e.get("grid_kwh", 0.0) for e in hp_sorted)
    total_cost_calc = sum(
        e.get("grid_kwh", 0.0) * hours_by_hour.get(e.get("hour"), {}).get("tariff_bdt_per_kwh", 0.0)
        for e in hp_sorted
    )
    peak_grid_calc = max(e.get("grid_kwh", 0.0) for e in hp_sorted)

    reported_grid = data.get("total_grid_kwh", 0.0)
    reported_cost = data.get("total_cost_bdt", 0.0)
    reported_peak = data.get("peak_grid_kwh", 0.0)

    if abs(reported_grid - total_grid_calc) > TOLERANCE:
        failures.append(f"total_grid_kwh mismatch: reported {reported_grid:.4f}, calculated {total_grid_calc:.4f}")
    if abs(reported_cost - total_cost_calc) > TOLERANCE:
        failures.append(f"total_cost_bdt mismatch: reported {reported_cost:.4f}, calculated {total_cost_calc:.4f}")
    if abs(reported_peak - peak_grid_calc) > TOLERANCE:
        failures.append(f"peak_grid_kwh mismatch: reported {reported_peak:.4f}, calculated {peak_grid_calc:.4f}")

    return failures


def main():
    parser = argparse.ArgumentParser(description="GridPilot public sample test runner")
    parser.add_argument("--url", default="http://localhost:8000", help="Service base URL")
    parser.add_argument(
        "--file",
        default=str(Path(__file__).parent / "public_samples.json"),
        help="Path to the public samples JSON file",
    )
    args = parser.parse_args()

    sample_path = Path(args.file)
    if not sample_path.exists():
        print(f"[ERROR] Sample file not found: {sample_path}")
        sys.exit(1)

    with open(sample_path, encoding="utf-8") as f:
        samples = json.load(f)

    if isinstance(samples, dict):
        # Handle {"cases": [...]} wrapper — official BUP format has top-level "cases"
        for key in ("cases", "scenarios", "samples"):
            if key in samples and isinstance(samples[key], list):
                samples = samples[key]
                break
        else:
            samples = list(samples.values())

    # Official BUP format: each element is {"id": ..., "label": ..., "input": {...}, "expected_output": {...}}
    # Normalise: extract just the "input" payload (what we POST), keeping "id" as the scenario label.
    normalised = []
    for item in samples:
        if isinstance(item, dict) and "input" in item:
            payload = item["input"]
            # Use top-level id/label for reporting if scenario_id is absent
            if "scenario_id" not in payload and "id" in item:
                payload = dict(payload)
                payload["scenario_id"] = item["id"]
            normalised.append(payload)
        else:
            normalised.append(item)
    samples = normalised

    print(f"Running {len(samples)} case(s) against {args.url}\n")

    passed = 0
    failed = 0

    for i, case in enumerate(samples):
        sid = case.get("scenario_id", f"case_{i}")
        failures = check_case(case, args.url)
        if failures:
            failed += 1
            print(f"[FAIL] {sid}")
            for msg in failures:
                print(f"       ✗ {msg}")
        else:
            passed += 1
            print(f"[PASS] {sid}")

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(samples)} cases.")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

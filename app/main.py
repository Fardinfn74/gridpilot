"""
main.py — FastAPI application entry point for GridPilot AI.

Endpoints:
  GET  /health           → {"status": "ok"}
  POST /optimize-energy  → OptimizeResponse (see schemas.py)

The pipeline:
  request → LLM interpreter → guardrail validator
          → MILP optimizer → final validator → response
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app import config
from app.guardrails import validate_and_clean
from app.llm_interpreter import interpret_notes
from app.optimizer import DirectiveConstraints, build_directive_constraints, solve_milp
from app.schemas import (
    DirectiveInterpretation,
    ErrorResponse,
    OptimizeRequest,
    OptimizeResponse,
)
from app.validator import PlanValidationError, recompute_totals, validate_plan

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="GridPilot AI",
    description="LLM-assisted 24-hour campus energy optimizer (GridWise LLM)",
    version="1.0.0",
)

# Serve the web dashboard from /
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# CORS — allow calls from any origin (judges, browser tools, etc.)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Baseline endpoint — naive plan (no battery dispatch, all demand from grid
# minus solar) used for the savings comparison widget in the dashboard.
# ---------------------------------------------------------------------------
@app.post("/baseline-cost")
async def baseline_cost(request: Request):
    """Compute the naive baseline cost: use all available solar, pull the
    rest from grid, no battery. Returns total_cost_bdt and total_grid_kwh."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    hours = body.get("hours", [])
    if len(hours) != 24:
        return JSONResponse(status_code=400, content={"error": "Need 24 hours"})

    total_cost = 0.0
    total_grid = 0.0
    for h in hours:
        solar = min(float(h.get("solar_kwh", 0)), float(h.get("demand_kwh", 0)))
        grid = max(0.0, float(h.get("demand_kwh", 0)) - solar)
        cost = grid * float(h.get("tariff_bdt_per_kwh", 0))
        total_grid += grid
        total_cost += cost

    return {
        "total_grid_kwh": round(total_grid, 4),
        "total_cost_bdt": round(total_cost, 4),
    }


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(ValidationError)
async def pydantic_validation_error_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "Request validation failed", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Core pipeline (async wrapper with timeout)
# ---------------------------------------------------------------------------

async def _run_pipeline(req: OptimizeRequest) -> OptimizeResponse:
    """
    Run the full GridWise pipeline:
      interpret → guardrails → optimize → validate → respond
    Wrapped in an asyncio timeout (REQUEST_TIMEOUT_SECONDS).
    """
    loop = asyncio.get_event_loop()

    # --- Step 1: LLM Interpretation (blocking I/O → run in thread pool) ---
    try:
        raw_entries = await asyncio.wait_for(
            loop.run_in_executor(None, interpret_notes, req.operator_notes),
            timeout=config.LLM_TIMEOUT_SECONDS + 2,
        )
    except asyncio.TimeoutError:
        logger.warning("LLM call timed out — falling back to all-no_op directives.")
        raw_entries = []
    except RuntimeError as exc:
        logger.warning("LLM interpretation failed: %s — falling back to no_op.", exc)
        raw_entries = []

    # --- Step 2: Guardrail Validation ---
    cleaned_directives = validate_and_clean(
        raw_entries, len(req.operator_notes), req.battery
    )

    # --- Step 3: Build directive constraints ---
    dc = build_directive_constraints(req.hours, req.battery, cleaned_directives)

    # --- Step 4: MILP Optimization (blocking CPU → run in thread pool) ---
    try:
        hourly_plan = await asyncio.wait_for(
            loop.run_in_executor(None, solve_milp, req.hours, req.battery, dc),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        raise RuntimeError("Optimizer timed out — could not produce a plan within time limit.")
    except RuntimeError as exc:
        raise RuntimeError(f"Optimizer failed: {exc}") from exc

    # --- Step 5: Final Replay Validation ---
    validate_plan(hourly_plan, req.hours, req.battery)

    # --- Step 6: Recompute totals (NEVER trust optimizer internals) ---
    total_grid, total_cost, peak_grid = recompute_totals(hourly_plan, req.hours)

    # --- Step 7: Build plan summary ---
    n_charge = sum(1 for e in hourly_plan if e.battery_action == "charge")
    n_discharge = sum(1 for e in hourly_plan if e.battery_action == "discharge")
    active_directives = [d for d in cleaned_directives if d.get("applies")]
    summary = (
        f"24-hour plan: grid cost {total_cost:.2f} BDT over {total_grid:.2f} kWh "
        f"(peak {peak_grid:.2f} kWh/h). "
        f"Battery charged {n_charge}h, discharged {n_discharge}h. "
        f"{len(active_directives)} of {len(cleaned_directives)} operator directive(s) applied."
    )

    # --- Build response ---
    directive_interps = [
        DirectiveInterpretation(**d) for d in cleaned_directives
    ]

    return OptimizeResponse(
        scenario_id=req.scenario_id,
        directive_interpretation=directive_interps,
        hourly_plan=hourly_plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=summary,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(request: Request):
    # Parse body — return 400 on JSON/schema errors
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON in request body"},
        )

    try:
        req = OptimizeRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "Request schema validation failed", "detail": exc.errors()},
        )

    # Run the full pipeline with an overall timeout
    try:
        response = await asyncio.wait_for(
            _run_pipeline(req),
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        return response
    except asyncio.TimeoutError:
        logger.error("Request timed out for scenario_id=%s", req.scenario_id)
        return JSONResponse(
            status_code=500,
            content={"error": "Request timed out — could not complete within 25 seconds"},
        )
    except PlanValidationError as exc:
        logger.error("Plan validation failed for scenario_id=%s: %s", req.scenario_id, exc)
        return JSONResponse(
            status_code=500,
            content={"error": "Plan validation failed", "detail": str(exc)},
        )
    except RuntimeError as exc:
        logger.error("Pipeline error for scenario_id=%s: %s", req.scenario_id, exc)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal pipeline error"},
        )
    except Exception as exc:
        logger.error(
            "Unexpected error for scenario_id=%s: %s\n%s",
            req.scenario_id,
            exc,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )

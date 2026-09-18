# GridPilot AI — GridWise LLM Preliminary Submission

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Railway-blueviolet?style=for-the-badge)](https://gridpilot-production.up.railway.app/)
[![API Health](https://img.shields.io/badge/API-Online-brightgreen?style=for-the-badge)](https://gridpilot-production.up.railway.app/health)

## 🔗 Live Demo
**→ [https://gridpilot-production.up.railway.app/](https://gridpilot-production.up.railway.app/)**

> Open the link, click **⚡ Load Sample**, then click **Optimize 24-Hour Plan** to see the full pipeline in action.

## What this is

LLM-assisted 24-hour campus energy optimizer. Interprets natural-language operator notes into structured
directives (LLM), validates them deterministically (guardrails), and solves a cost-minimizing 24-hour
battery/grid/solar schedule (MILP optimizer).

## Model / provider

- **LLM:** Google Gemini 2.5 Flash (`gemini-2.5-flash`), used exclusively to produce
  `directive_interpretation` from `operator_notes`. 5-key fallback pool for reliability under load.
- **Optimizer:** PuLP + CBC (MILP), minimizes total grid cost subject to GridWise + directive constraints.


## Architecture

```
operator_notes
     │
     ▼
LLM Interpreter    (LLM call → JSON-only output, one entry per note, 1 retry)
     │
     ▼
Guardrail Validator (deterministic: schema, range, hour-list checks, merge overlapping directives,
                     safe fallback to no_op on any violation — never crashes)
     │
     ▼
Directive Constraints (merged effective_solar, min_reserve, no-charge/discharge sets, max_grid)
     │
     ▼
MILP Optimizer     (PuLP/CBC: minimize Σ grid[h]·tariff[h], 24 binary + continuous variables)
     │
     ▼
Final Replay Validator (hour-by-hour balance, battery bounds, end-of-day neutrality)
     │
     ▼
Recompute Totals   (total_grid_kwh / total_cost_bdt / peak_grid_kwh from hourly_plan, never from optimizer)
     │
     ▼
API Response
```

## Environment variables

| Name | Purpose | Default |
|---|---|---|
| `LLM_API_KEY` | Credential for the LLM provider | *(required)* |
| `LLM_MODEL` | Model identifier | `claude-3-5-haiku-20241022` |
| `LLM_PROVIDER` | `anthropic` or `openai` | `anthropic` |
| `PORT` | Service port | `8000` |
| `LLM_TIMEOUT_SECONDS` | LLM call hard timeout | `10` |
| `REQUEST_TIMEOUT_SECONDS` | Total request budget | `25` |

## Run locally

```bash
git clone <repo> && cd gridpilot
cp .env.example .env    # fill in LLM_API_KEY (and LLM_PROVIDER/LLM_MODEL if not using Anthropic)
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Verify

```bash
curl http://localhost:8000/health
# → {"status":"ok"}

curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @tests/public_samples.json
```

Or use one of the case objects from `tests/public_samples.json` directly:

```bash
python -c "
import json, httpx
cases = json.load(open('tests/public_samples.json'))['cases']
r = httpx.post('http://localhost:8000/optimize-energy', json=cases[0], timeout=35)
print(r.status_code, json.dumps(r.json(), indent=2)[:500])
"
```

## Run public sample suite

```bash
python tests/test_public_samples.py
# or with custom URL / file:
python tests/test_public_samples.py --url http://localhost:8000 --file path/to/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json
```

## Docker fallback

```bash
# Build
docker build -t gridpilot:latest .

# Run
docker run -p 8000:8000 \
  -e LLM_API_KEY=sk-... \
  -e LLM_MODEL=claude-3-5-haiku-20241022 \
  -e LLM_PROVIDER=anthropic \
  gridpilot:latest

# Verify
curl http://localhost:8000/health
```

Or pull from registry (fill in your registry/tag after pushing):

```bash
docker pull <registry>/gridpilot:<tag>
docker run -p 8000:8000 -e LLM_API_KEY=... -e LLM_MODEL=... <registry>/gridpilot:<tag>
```

## Directive types supported

| Type | Effect |
|---|---|
| `solar_reduction` | `effective_solar[h] = solar_kwh[h] × factor` |
| `minimum_battery_reserve` | `battery_energy_after[h] >= directive_min` |
| `no_charge_window` | `charge[h] = 0` |
| `no_discharge_window` | `discharge[h] = 0` |
| `max_grid_window` | `grid[h] <= max_grid_kwh` |
| `no_op` | No effect (applies=false) |

Overlapping same-type directives are merged (most restrictive): factors multiplied, reserves maximized,
max_grid minimized, charge/discharge windows unioned.

## Known limitations

- Single-region deployment; no caching of LLM results across requests.
- No authentication layer (as required by the problem statement — judges call directly).
- OpenAI `response_format: json_object` mode requires a mention of "JSON" in the prompt; covered by
  system prompt.
- Docker image uses the system CBC solver (`coinor-cbc`); PuLP auto-detects it.

## Credits

- [FastAPI](https://fastapi.tiangolo.com/) — web framework
- [PuLP](https://coin-or.github.io/pulp/) — LP/MILP modelling
- [Anthropic Claude](https://docs.anthropic.com/) / [OpenAI](https://platform.openai.com/) — LLM backend
- [Pydantic v2](https://docs.pydantic.dev/) — schema validation

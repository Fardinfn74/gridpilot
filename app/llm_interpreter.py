"""
llm_interpreter.py — LLM interpreter for GridPilot AI.

Sends operator notes to an LLM (Anthropic Claude or OpenAI) and parses the
JSON directive_interpretation array. One retry with error-correction message
on parse failure or wrong entry count.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from app import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You convert campus-operator notes into structured energy-schedule directives. Output ONLY a JSON array,
no prose, no markdown fences.

Return exactly one object per input note, in the same order, with this shape:
{"note_index": int, "applies": bool, "directive_type": string, "structured_adjustment": object|null, "explanation": string}

Allowed directive_type values and required structured_adjustment shape:
- solar_reduction: {"hours":[int...], "factor": number}   // factor = usable fraction REMAINING (an 80% reduction = 0.2)
- minimum_battery_reserve: {"hours":[int...], "minimum_energy_kwh": number}
- no_charge_window: {"hours":[int...]}
- no_discharge_window: {"hours":[int...]}
- max_grid_window: {"hours":[int...], "max_grid_kwh": number}
- no_op: null   // use when the note does not affect today's 24-hour energy schedule

Rules:
- hours arrays: unique integers 0-23, ascending, start-inclusive/end-exclusive ("1 PM to 3 PM" -> [13,14]).
- applies must be false ONLY for no_op; every other directive_type requires applies = true.
- Never invent a directive_type outside the six listed above.
- Never change base demand, tariff, or battery parameters — only emit the six directive shapes above.
- If a note is ambiguous or does not clearly match a supported directive, use no_op rather than guessing.
- Paraphrases, percentages, and different time phrasings can describe the same directive — extract the
  underlying rule, not the exact wording.

Examples:
Note: "Solar output will drop to about 20% from 1 PM to 3 PM."
-> {"directive_type":"solar_reduction","structured_adjustment":{"hours":[13,14],"factor":0.2}, ...}
Note: "Do not charge the battery between 2 PM and 4 PM."
-> {"directive_type":"no_charge_window","structured_adjustment":{"hours":[14,15]}, ...}
Note: "Keep at least 120 kWh in reserve from 6 PM until 9 PM."
-> {"directive_type":"minimum_battery_reserve","structured_adjustment":{"hours":[18,19,20],"minimum_energy_kwh":120}, ...}
Note: "The cafeteria menu changes tomorrow."
-> {"directive_type":"no_op","structured_adjustment":null, ...}
"""


def _format_notes(operator_notes: List[str]) -> str:
    return "\n".join(f'{i}: "{note}"' for i, note in enumerate(operator_notes))


def _call_anthropic(system: str, user_message: str, timeout: float) -> str:
    """Call Anthropic Claude API and return the raw text response."""
    import anthropic

    client = anthropic.Anthropic(api_key=config.LLM_API_KEY)
    response = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
        timeout=timeout,
    )
    return response.content[0].text


def _call_google(system: str, user_message: str, timeout: float) -> str:
    """Call Google AI Studio (Gemini) cycling through the key pool on quota/auth errors."""
    from google import genai
    from google.genai import types

    keys = config.LLM_API_KEY_POOL or [config.LLM_API_KEY]
    last_exc: Exception = RuntimeError("No API keys configured.")

    for idx, api_key in enumerate(keys):
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=config.LLM_MODEL,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=1024,
                    temperature=0.0,
                ),
            )
            if idx > 0:
                logger.info("LLM: key #%d succeeded after %d failure(s).", idx + 1, idx)
            return response.text
        except Exception as exc:
            err_str = str(exc).lower()
            # Only fall through to next key on quota/rate-limit/auth errors
            if any(kw in err_str for kw in ("quota", "rate", "429", "403", "resource exhausted", "api key")):
                logger.warning("LLM: key #%d failed (%s), trying next key…", idx + 1, type(exc).__name__)
                last_exc = exc
                continue
            # Any other error (parse, network, etc.) — raise immediately
            raise

    raise last_exc



def _call_openrouter(system: str, user_message: str, timeout: float) -> str:
    """Call OpenRouter API (OpenAI-compatible) and return the raw text response."""
    from openai import OpenAI

    client = OpenAI(
        api_key=config.LLM_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
    response = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        timeout=timeout,
        max_tokens=1024,
    )
    return response.choices[0].message.content


def _call_openai(system: str, user_message: str, timeout: float) -> str:
    """Call OpenAI API and return the raw text response."""
    from openai import OpenAI

    client = OpenAI(api_key=config.LLM_API_KEY)
    response = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        timeout=timeout,
        max_tokens=1024,
    )
    return response.choices[0].message.content


def _call_llm(system: str, user_message: str) -> str:
    """Dispatch to the configured LLM provider."""
    timeout = config.LLM_TIMEOUT_SECONDS
    provider = config.LLM_PROVIDER.lower()

    if provider == "google":
        return _call_google(system, user_message, timeout)
    elif provider == "openrouter":
        return _call_openrouter(system, user_message, timeout)
    elif provider == "openai":
        return _call_openai(system, user_message, timeout)
    else:
        # Default: anthropic
        return _call_anthropic(system, user_message, timeout)


def _parse_json_array(raw: str, expected_count: int) -> List[Dict[str, Any]]:
    """
    Parse the raw LLM output as a JSON array.
    Strips any accidental markdown fences before parsing.
    Raises ValueError if parsing fails or count doesn't match.
    """
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        text = text.strip()

    data = json.loads(text)

    if isinstance(data, dict):
        # Some models wrap array in {"directives": [...]} etc.
        for key in ("directives", "interpretations", "result", "results"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            raise ValueError("Expected JSON array but got object with no known wrapper key")

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    if len(data) != expected_count:
        raise ValueError(
            f"Expected {expected_count} entries but got {len(data)}"
        )

    return data


def interpret_notes(operator_notes: List[str]) -> List[Dict[str, Any]]:
    """
    Call the LLM to interpret operator_notes into directive entries.
    Returns a list of raw dict entries (before guardrail validation).
    On LLM or parse failure, raises RuntimeError so caller can handle gracefully.
    """
    n = len(operator_notes)
    notes_text = _format_notes(operator_notes)
    user_message = notes_text

    try:
        raw = _call_llm(SYSTEM_PROMPT, user_message)
        result = _parse_json_array(raw, n)
        logger.info("LLM interpretation succeeded on first attempt.")
        return result

    except (ValueError, json.JSONDecodeError) as first_err:
        logger.warning(
            "LLM first attempt failed (%s). Retrying with error-correction prompt.",
            first_err,
        )
        # Retry once with error correction message
        correction_message = (
            f"{notes_text}\n\n"
            f"Previous response could not be parsed: {first_err}\n"
            "Please respond with ONLY a valid JSON array of exactly "
            f"{n} objects following the format above. No prose, no fences."
        )
        try:
            raw2 = _call_llm(SYSTEM_PROMPT, correction_message)
            result2 = _parse_json_array(raw2, n)
            logger.info("LLM interpretation succeeded on retry.")
            return result2
        except (ValueError, json.JSONDecodeError) as second_err:
            logger.error("LLM retry also failed: %s", second_err)
            raise RuntimeError(
                f"LLM failed to produce valid JSON after 2 attempts: {second_err}"
            ) from second_err

    except Exception as exc:
        logger.error("LLM call raised unexpected error: %s", exc)
        raise RuntimeError(f"LLM call failed: {exc}") from exc

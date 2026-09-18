"""
config.py — Environment variable loading for GridPilot AI.
All sensitive credentials are read exclusively from environment variables.
No secrets are hardcoded here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# LLM provider settings
LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
LLM_MODEL: str = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "google")  # "google" | "openrouter" | "anthropic" | "openai"

# Optional pool of keys — comma-separated. If set, the interpreter will
# cycle through them on quota/rate-limit errors for maximum reliability.
_pool_raw: str = os.environ.get("LLM_API_KEY_POOL", "")
LLM_API_KEY_POOL: list[str] = (
    [k.strip() for k in _pool_raw.split(",") if k.strip()]
    if _pool_raw
    else ([LLM_API_KEY] if LLM_API_KEY else [])
)

# Service settings
PORT: int = int(os.environ.get("PORT", "8000"))

# Timeout settings (keep well under the 30s judge hard limit)
LLM_TIMEOUT_SECONDS: float = float(os.environ.get("LLM_TIMEOUT_SECONDS", "10"))
REQUEST_TIMEOUT_SECONDS: float = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "25"))

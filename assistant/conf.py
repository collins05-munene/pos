"""
Settings for the assistant, read at call time so they can be overridden in tests.
Put real values in environment variables (Render -> Environment), never in code.
"""
from django.conf import settings


def _get(name, default):
    return getattr(settings, name, default)


def api_key():
    return (_get("GEMINI_API_KEY", "") or "").strip()


def model():
    # Model names get retired often. Keep this in an env var so you can change it
    # without a deploy. Verify what your key can use with:  manage.py assistant_check --list-models
    return _get("GEMINI_MODEL", "gemini-flash-lite-latest")


def enabled():
    return bool(_get("ASSISTANT_ENABLED", True)) and bool(api_key())


def name():
    return _get("ASSISTANT_NAME", "POS Assistant")


def product_name():
    return _get("ASSISTANT_PRODUCT_NAME", "this POS system")


def temperature():
    return float(_get("ASSISTANT_TEMPERATURE", 0.3))


def max_output_tokens():
    # Reasoning ("thinking") tokens count against this, so keep headroom.
    return int(_get("ASSISTANT_MAX_OUTPUT_TOKENS", 1500))


def thinking_level():
    # Optional, e.g. "minimal" or "low" on Gemini 3 models (faster, cheaper). Blank = model default.
    return (_get("GEMINI_THINKING_LEVEL", "") or "").strip()


def timeout_seconds():
    return int(_get("ASSISTANT_TIMEOUT_SECONDS", 25))


def tenant_daily_limit():
    # Protects the (small) free quota from one shop using it all. 0 = unlimited.
    return int(_get("ASSISTANT_DAILY_LIMIT_PER_TENANT", 100))

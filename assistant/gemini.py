"""Minimal Google Gemini client using only the standard library (no extra dependency)."""
import json
import logging
import socket
import urllib.error
import urllib.request

from . import conf

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(Exception):
    """user_message is safe to show to the shop's staff; details go to the log only."""

    def __init__(self, user_message, http_status=502):
        super().__init__(user_message)
        self.user_message = user_message
        self.http_status = http_status


def _request(method, path, body=None):
    key = conf.api_key()
    if not key:
        raise GeminiError("The assistant isn't set up yet. Please contact support.", 503)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{API_ROOT}/{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},  # key in header, never in the URL
    )
    try:
        with urllib.request.urlopen(req, timeout=conf.timeout_seconds()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            err = json.loads(exc.read(4000).decode("utf-8", "replace")).get("error", {})
            detail = f"{err.get('status', '')}: {err.get('message', '')}"[:300]
        except Exception:
            pass
        _raise_for_http(exc.code, detail)
    except (socket.timeout, TimeoutError):
        raise GeminiError("The assistant took too long to answer. Please try again.", 504)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise GeminiError("The assistant took too long to answer. Please try again.", 504)
        log.warning("Gemini network error: %s", exc.reason)
        raise GeminiError("Couldn't reach the assistant. Check your internet connection and try again.", 502)
    except ValueError:
        log.error("Gemini returned a non-JSON response")
        raise GeminiError("The assistant sent back something unreadable. Please try again.", 502)


def _raise_for_http(code, detail):
    if code == 429:
        log.warning("Gemini quota/rate limit hit: %s", detail)
        raise GeminiError("The assistant is very busy right now. Please try again in a minute.", 429)
    if code in (400, 401, 403):
        log.error("Gemini rejected the request (%s) - check GEMINI_API_KEY / GEMINI_MODEL: %s", code, detail)
        raise GeminiError("The assistant is temporarily unavailable. Please contact support if this continues.", 503)
    if code == 404:
        log.error("Gemini model '%s' not found - it may have been retired. Run: manage.py assistant_check --list-models. %s",
                  conf.model(), detail)
        raise GeminiError("The assistant is temporarily unavailable. Please contact support if this continues.", 503)
    log.error("Gemini server error %s: %s", code, detail)
    raise GeminiError("The assistant had a problem. Please try again shortly.", 502)


def build_body(system_prompt, contents):
    generation = {"temperature": conf.temperature(), "maxOutputTokens": conf.max_output_tokens()}
    level = conf.thinking_level()
    if level:
        generation["thinkingConfig"] = {"thinkingLevel": level}
    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": generation,
    }


def extract_text(data):
    """Pull the visible answer out of a generateContent response."""
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise GeminiError("I can't help with that request. Try rephrasing your question about the POS.", 200)

    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError("I couldn't come up with an answer. Please try rephrasing.", 502)

    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought")).strip()
    if text:
        if cand.get("finishReason") == "MAX_TOKENS":
            text += " …"
        return text
    if cand.get("finishReason") == "MAX_TOKENS":
        raise GeminiError("My answer got cut off. Please ask a shorter or more specific question.", 502)
    raise GeminiError("I couldn't come up with an answer. Please try rephrasing.", 502)


def generate(system_prompt, contents):
    """contents = [{"role": "user"|"model", "parts": [{"text": "..."}]}, ...] -> answer text."""
    data = _request("POST", f"models/{conf.model()}:generateContent", build_body(system_prompt, contents))
    return extract_text(data)


def list_models():
    """Names of models this key can call generateContent on (used by assistant_check)."""
    data = _request("GET", "models?pageSize=200")
    return sorted(
        m["name"].split("/", 1)[-1]
        for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    )

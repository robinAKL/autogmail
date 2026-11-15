"""One way to call the model, shared by replies, summaries and the WhatsApp chat."""

import json
import re

from gmail_auto.envfile import VERCEL_GATEWAY_URL, openai_base_url, openai_model, require_openai_key
from gmail_auto.errors import ReplyError


def active_model() -> str:
    """Return the selected Settings model, or fall back to OPENAI_MODEL / gpt-4o-mini."""
    try:
        from gmail_auto.settings import load_settings

        chosen = (load_settings().get("ai_model") or "").strip()
        if chosen:
            return chosen
    except Exception:
        pass
    return openai_model()


def resolve_model(model: str, base_url: str) -> str:
    """Adapt provider/model IDs for Vercel AI Gateway vs direct OpenAI."""
    if base_url == VERCEL_GATEWAY_URL:
        return model if "/" in model else f"openai/{model}"
    if base_url:
        return model
    if model.startswith("openai/"):
        return model.split("/", 1)[1]
    if "/" in model:
        raise ReplyError(f"Model {model} requires AI_GATEWAY_API_KEY (or OPENAI_BASE_URL).")
    return model


def _is_reasoning_model(model: str) -> bool:
    bare = model.split("/", 1)[1] if "/" in model else model
    return bare.startswith("gpt-5")


def complete(system: str, user: str, max_tokens: int = 900, temperature: float = 0.3) -> str:
    try:
        from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError
    except ImportError as exc:
        raise ReplyError("The openai library is not installed. Run: pip install -r requirements.txt") from exc

    kwargs = {"api_key": require_openai_key(), "timeout": 60.0, "max_retries": 0}
    base_url = openai_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    model = resolve_model(active_model(), base_url)
    client = OpenAI(**kwargs)
    req = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if _is_reasoning_model(model):
        req["reasoning_effort"] = "low"
        req["max_completion_tokens"] = max(max_tokens, 2000)
    else:
        req["temperature"] = temperature
        req["max_tokens"] = max_tokens
    try:
        response = client.chat.completions.create(**req)
    except AuthenticationError as exc:
        raise ReplyError("The AI API key was rejected. Check AI_GATEWAY_API_KEY or OPENAI_API_KEY.") from exc
    except RateLimitError as exc:
        raise ReplyError("The AI provider is rate limiting requests. Try again later.") from exc
    except APIConnectionError as exc:
        raise ReplyError("Could not reach the AI provider.") from exc
    except APIStatusError as exc:
        code = getattr(exc, "status_code", "")
        raise ReplyError(f"The AI provider returned an error ({code}) for {model}: {exc}") from exc
    text = _message_text(response)
    if not text.strip():
        raise ReplyError("The model returned an empty reply. Try another model or retry.")
    return text


def parse_json(text: str) -> dict | None:
    """Models wrap JSON in fences or prose often enough that it is worth digging for."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    for candidate in (cleaned, _outer_braces(cleaned)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _outer_braces(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return ""
    return text[start : end + 1]


def _message_text(response) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise ReplyError("The model returned nothing.") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(getattr(part, "text", "") or ""))
        return "".join(parts)
    return ""

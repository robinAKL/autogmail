"""One way to call the model, shared by replies, summaries and the WhatsApp chat."""

import json
import os
import re

from gmail_auto.envfile import VERCEL_GATEWAY_URL, openai_base_url, openai_model, require_openai_key
from gmail_auto.errors import ReplyError


def model_ready() -> bool:
    """True when a real API key is configured. Dummy test keys do not count."""
    from gmail_auto.envfile import ai_gateway_key

    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    gateway = ai_gateway_key()
    if not key and not gateway:
        return False
    if key.startswith("test-key") or key.startswith("sk-test") or key == "sk-custom":
        return False
    return True


def active_model(role: str = "worker") -> str:
    """Worker writes and summarises; Super decides intent and what to do next."""
    try:
        from gmail_auto.settings import load_settings

        settings = load_settings()
        if role == "super":
            chosen = (settings.get("super_model") or "").strip()
            if chosen:
                return chosen
            return "openai/gpt-5-mini"
        chosen = (settings.get("ai_model") or "").strip()
        if chosen:
            return chosen
    except Exception:
        if role == "super":
            return "openai/gpt-5-mini"
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


def complete(system: str, user: str, max_tokens: int = 900, temperature: float = 0.3, role: str = "worker") -> str:
    try:
        from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError
    except ImportError as exc:
        raise ReplyError("The openai library is not installed. Run: pip install -r requirements.txt") from exc

    kwargs = {"api_key": require_openai_key(), "timeout": 60.0, "max_retries": 0}
    base_url = openai_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    model = resolve_model(active_model(role), base_url)
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


TRANSCRIBE_MODELS = ("gpt-4o-mini-transcribe", "whisper-1")


def transcribe(audio: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> str:
    """Turn a voice note into text. Prefers a direct OpenAI key, since gateways may not carry audio."""
    try:
        from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI
    except ImportError as exc:
        raise ReplyError("The openai library is not installed. Run: pip install -r requirements.txt") from exc
    if not audio:
        raise ReplyError("The voice message was empty.")

    direct = (os.getenv("OPENAI_API_KEY") or "").strip()
    explicit_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
    if direct:
        kwargs = {"api_key": direct}
        if explicit_url:
            kwargs["base_url"] = explicit_url
    else:
        kwargs = {"api_key": require_openai_key()}
        base_url = openai_base_url()
        if base_url:
            kwargs["base_url"] = base_url
    client = OpenAI(timeout=60.0, max_retries=0, **kwargs)
    last: Exception | None = None
    for model in TRANSCRIBE_MODELS:
        try:
            result = client.audio.transcriptions.create(model=model, file=(filename, audio, mime))
        except AuthenticationError as exc:
            raise ReplyError("The AI API key was rejected while reading the voice message.") from exc
        except APIConnectionError as exc:
            raise ReplyError("Could not reach the AI provider to read the voice message.") from exc
        except APIStatusError as exc:
            last = exc
            continue
        text = (getattr(result, "text", "") or "").strip()
        if text:
            return text
    if last is not None:
        raise ReplyError(f"Could not read the voice message ({getattr(last, 'status_code', '')}).") from last
    raise ReplyError("Could not hear any words in the voice message.")


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

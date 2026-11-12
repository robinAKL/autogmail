"""One way to call the model, shared by replies, summaries and the WhatsApp chat."""

import json
import re

from gmail_auto.envfile import openai_base_url, openai_model, require_openai_key
from gmail_auto.errors import ReplyError


def complete(system: str, user: str, max_tokens: int = 900, temperature: float = 0.3) -> str:
    try:
        from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError
    except ImportError as exc:
        raise ReplyError("The openai library is not installed. Run: pip install -r requirements.txt") from exc

    kwargs = {"api_key": require_openai_key(), "timeout": 60.0, "max_retries": 0}
    base_url = openai_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    try:
        response = client.chat.completions.create(
            model=openai_model(),
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except AuthenticationError as exc:
        raise ReplyError("The OpenAI key was rejected.") from exc
    except RateLimitError as exc:
        raise ReplyError("OpenAI is rate limiting requests. Try again later.") from exc
    except APIConnectionError as exc:
        raise ReplyError("Could not reach OpenAI.") from exc
    except APIStatusError as exc:
        code = getattr(exc, "status_code", "")
        raise ReplyError(f"OpenAI returned an error ({code}). Check OPENAI_MODEL and the key.") from exc
    return _message_text(response)


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

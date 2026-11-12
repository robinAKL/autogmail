import json
import re

from gmail_auto.errors import ConfigError, ProfileError
from gmail_auto.paths import atomic_write, root
from gmail_auto.storage import load_doc, save_doc
from gmail_auto.token_store import storage_ready

_SECRET = re.compile(r"sk-[A-Za-z0-9]{10,}|BEGIN PRIVATE KEY")
_PLACEHOLDER_NAMES = {"", "示例用户", "example user", "your name"}
_PLACEHOLDER_MARKS = ("请填写", "请用几句", "请描述", "(please fill", "(describe", "(a few facts")


def profile_path():
    return root() / "profile.json"


def clean_profile(data: object) -> dict:
    if not isinstance(data, dict):
        raise ProfileError("The profile has the wrong format.")
    cleaned = {
        "name": _text(data.get("name"), "Enter your name.", 80),
        "role": _text(data.get("role"), "Enter your role.", 120),
        "introduction": _text(data.get("introduction"), "Enter a short introduction.", 4000),
        "tone": _text(data.get("tone"), "Describe your tone.", 1000),
        "cannot_promise": _lines(data.get("cannot_promise"), "Write at least one thing you cannot promise."),
        "sample_phrases": _lines(data.get("sample_phrases"), "Write at least one phrase you would say."),
    }
    blob = json.dumps(cleaned, ensure_ascii=False)
    if _SECRET.search(blob):
        raise ProfileError("Do not put API keys or private keys in the profile.")
    return cleaned


def load_profile() -> dict:
    if storage_ready():
        saved = load_doc("profile", None)
        if isinstance(saved, dict):
            return clean_profile(saved)
    path = profile_path()
    if not path.exists():
        raise ConfigError("profile.json was not found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("profile.json is not valid JSON. Save the profile again from the Settings page.") from exc
    return clean_profile(data)


def save_profile(data: dict) -> dict:
    cleaned = clean_profile(data)
    if storage_ready():
        save_doc("profile", cleaned)
        return cleaned
    try:
        atomic_write(profile_path(), json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise ConfigError("The profile could not be saved here. Connect Vercel KV so hosted changes persist.") from exc
    return cleaned


def profile_from_form(form) -> dict:
    return clean_profile(
        {
            "name": form.get("name", ""),
            "role": form.get("role", ""),
            "introduction": form.get("introduction", ""),
            "tone": form.get("tone", ""),
            "cannot_promise": _split_lines(form.get("cannot_promise", "")),
            "sample_phrases": _split_lines(form.get("sample_phrases", "")),
        }
    )


def profile_is_placeholder(profile: dict) -> bool:
    if profile.get("name", "").strip().lower() in _PLACEHOLDER_NAMES:
        return True
    for key in ("role", "introduction", "tone"):
        text = profile.get(key, "").lower()
        if any(mark in text for mark in _PLACEHOLDER_MARKS):
            return True
    return False


def profile_as_prompt(profile: dict) -> str:
    cannot = "\n".join(f"- {item}" for item in profile["cannot_promise"])
    phrases = "\n".join(f"- {item}" for item in profile["sample_phrases"])
    return (
        "Name: " + profile["name"] + "\n"
        "Role: " + profile["role"] + "\n"
        "Introduction: " + profile["introduction"] + "\n"
        "Tone: " + profile["tone"] + "\n"
        "Things I cannot promise:\n" + cannot + "\n"
        "Phrases I would say:\n" + phrases
    )


def _text(value: object, message: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ProfileError(message)
    text = value.strip()
    if not text:
        raise ProfileError(message)
    if len(text) > limit:
        raise ProfileError("One of the fields is too long.")
    return text


def _lines(value: object, message: str) -> list[str]:
    if isinstance(value, str):
        items = _split_lines(value)
    elif isinstance(value, list):
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ProfileError("Write one item per line.")
            items.append(item.strip())
    else:
        raise ProfileError(message)
    if not items:
        raise ProfileError(message)
    if len(items) > 30:
        raise ProfileError("Too many items. Keep it to 30 or fewer.")
    for item in items:
        if len(item) > 200:
            raise ProfileError("One of the items is too long.")
    return items


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]

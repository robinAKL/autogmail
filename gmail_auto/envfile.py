import os

from gmail_auto.errors import ConfigError
from gmail_auto.paths import CODE_ROOT, root


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ConfigError("Dependencies are not installed. Run: pip install -r requirements.txt") from exc
    load_dotenv(CODE_ROOT / ".env")
    if root() != CODE_ROOT:
        load_dotenv(root() / ".env", override=True)


def require_openai_key() -> str:
    load_env()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise ConfigError("OPENAI_API_KEY was not found. Copy .env.example to .env and add your own key.")
    return key


def has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"


def openai_base_url() -> str:
    return os.getenv("OPENAI_BASE_URL", "").strip()

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


VERCEL_GATEWAY_URL = "https://ai-gateway.vercel.sh/v1"


def ai_gateway_key() -> str:
    return os.getenv("AI_GATEWAY_API_KEY", "").strip()


def using_gateway() -> bool:
    return bool(ai_gateway_key()) and not os.getenv("OPENAI_BASE_URL", "").strip()


def require_openai_key() -> str:
    load_env()
    explicit_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if explicit_url:
        key = os.getenv("OPENAI_API_KEY", "").strip() or ai_gateway_key()
    else:
        key = ai_gateway_key() or os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise ConfigError("Set AI_GATEWAY_API_KEY or OPENAI_API_KEY in .env.")
    return key


def has_openai_key() -> bool:
    return bool(ai_gateway_key() or os.getenv("OPENAI_API_KEY", "").strip())


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"


def openai_base_url() -> str:
    explicit = os.getenv("OPENAI_BASE_URL", "").strip()
    if explicit:
        return explicit
    if ai_gateway_key():
        return VERCEL_GATEWAY_URL
    return ""

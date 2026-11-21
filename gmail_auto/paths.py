import os
from pathlib import Path

from gmail_auto.tenant import require_user_id, saas_enabled

CODE_ROOT = Path(__file__).resolve().parent.parent


def root() -> Path:
    override = os.environ.get("AUTOMAIL_HOME")
    if override:
        return Path(override)
    return CODE_ROOT


def data_dir(create: bool = True, *, platform: bool = False) -> Path:
    path = root() / "data"
    if saas_enabled() and not platform:
        path = path / "users" / require_user_id()
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            if not os.environ.get("VERCEL"):
                raise
    return path


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)

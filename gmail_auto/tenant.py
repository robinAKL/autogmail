"""The authenticated SaaS user for the current request or job."""

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar

from gmail_auto.errors import ConfigError

_user_id = ContextVar("autogmail_user_id", default=None)


def saas_enabled() -> bool:
    return os.environ.get("AUTOMAIL_SAAS") == "1"


def current_user_id() -> str | None:
    return _user_id.get()


def require_user_id() -> str:
    user_id = current_user_id()
    if user_id is None:
        raise ConfigError("Sign in to access your workspace.")
    return user_id


@contextmanager
def user_scope(user_id: str):
    if not isinstance(user_id, str) or not re.fullmatch(r"[a-f0-9]{32}", user_id):
        raise ConfigError("Invalid workspace identity.")
    token = _user_id.set(user_id)
    try:
        yield
    finally:
        _user_id.reset(token)

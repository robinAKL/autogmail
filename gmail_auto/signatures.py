"""Per-mailbox signature logo helpers shared by Gmail and Outlook senders."""

from gmail_auto.settings import load_settings, signature_logo_bytes


def load_logo(provider: str, account: str = "") -> tuple[str, str, bytes] | None:
    """Return (filename, mime, bytes) for the bound (provider, account), or None."""
    loaded = signature_logo_bytes(load_settings(), provider=provider, email=account)
    if loaded is None:
        return None
    meta, data = loaded
    return str(meta.get("name") or "logo"), str(meta.get("mime") or "image/png"), data

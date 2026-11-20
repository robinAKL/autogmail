"""Per-mailbox signature logo helpers shared by Gmail and Outlook senders."""

import html

from gmail_auto.settings import (
    load_settings,
    normalize_logo_position,
    signature_for_mailbox,
    signature_logo_bytes,
    signature_logo_position_for_mailbox,
)


def load_logo(provider: str, account: str = "") -> tuple[str, str, bytes] | None:
    """Return (filename, mime, bytes) for the bound (provider, account), or None."""
    loaded = signature_logo_bytes(load_settings(), provider=provider, email=account)
    if loaded is None:
        return None
    meta, data = loaded
    return str(meta.get("name") or "logo"), str(meta.get("mime") or "image/png"), data


def load_logo_layout(provider: str, account: str = "") -> tuple[str, str]:
    """Return (signature_text, logo_position) for the bound (provider, account)."""
    try:
        settings = load_settings()
    except Exception:
        return "", "bottom"
    sig = signature_for_mailbox(settings, provider=provider, email=account)
    pos = signature_logo_position_for_mailbox(settings, provider=provider, email=account)
    return sig, pos


def split_body_and_signature(body: str, signature: str = "") -> tuple[str, str]:
    """Split trailing configured signature from body without touching main body text."""
    text = (body or "").rstrip()
    sig = (signature or "").strip()
    if not sig or not text.endswith(sig):
        return text, ""
    prefix = text[: len(text) - len(sig)]
    if prefix and not prefix.endswith(("\n", "\r")):
        return text, ""
    return prefix.rstrip(), sig


def _nl2br(text: str) -> str:
    return html.escape(text or "").replace("\n", "<br>")


def render_signature_block_html(sig_text: str, img_html: str, position: str = "bottom") -> str:
    """Arrange inline logo HTML around the signature text block only."""
    sig_inner = _nl2br(sig_text)
    if not img_html:
        return f'<div class="signature-block">{sig_inner}</div>' if sig_inner else ""
    if not sig_inner:
        return f'<div class="signature-block">{img_html}</div>'
    pos = normalize_logo_position(position)
    if pos == "top":
        return (
            '<div class="signature-block" style="margin-top:12px;">'
            f'<div style="margin-bottom:8px;">{img_html}</div>'
            f'<div>{sig_inner}</div>'
            "</div>"
        )
    if pos == "left":
        return (
            '<table class="signature-block" role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'style="margin-top:12px;border-collapse:collapse;"><tr>'
            f'<td style="vertical-align:top;padding-right:14px;">{img_html}</td>'
            f'<td style="vertical-align:top;">{sig_inner}</td>'
            "</tr></table>"
        )
    if pos == "right":
        return (
            '<table class="signature-block" role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'style="margin-top:12px;border-collapse:collapse;"><tr>'
            f'<td style="vertical-align:top;padding-right:14px;">{sig_inner}</td>'
            f'<td style="vertical-align:top;">{img_html}</td>'
            "</tr></table>"
        )
    return (
        '<div class="signature-block" style="margin-top:12px;">'
        f'<div>{sig_inner}</div>'
        f'<div style="margin-top:8px;">{img_html}</div>'
        "</div>"
    )


def render_body_with_logo_html(
    body: str,
    signature: str = "",
    img_html: str = "",
    position: str = "bottom",
    html_suffix: str = "",
) -> str:
    """Render full HTML body, positioning img_html only around the signature text block."""
    main_body, sig_block = split_body_and_signature(body, signature)
    parts: list[str] = []
    if main_body:
        parts.append(f"<div>{_nl2br(main_body)}</div>")
    sig_html = render_signature_block_html(sig_block, img_html, position=position)
    if sig_html:
        parts.append(sig_html)
    if html_suffix:
        parts.append(html_suffix)
    return "".join(parts)

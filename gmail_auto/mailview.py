"""Show a letter the way it was written, without letting it run anything.

The page is rendered in a sandboxed frame and served with a policy that blocks
scripts, forms and plugins. The cleaning below is a second layer on top of that.
"""

import html
import re

# What the frame may load: pictures, inline styles and fonts. Nothing that executes.
FRAME_POLICY = (
    "default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'; font-src https: data:; "
    "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox"
)
_BLOCKS = re.compile(r"(?is)<(script|iframe|frame|frameset|object|embed|applet|form|noscript)\b.*?</\1\s*>")
_LONE = re.compile(r"(?is)<(script|iframe|frame|object|embed|applet|base|link|meta)\b[^>]*>")
_CLOSERS = re.compile(r"(?is)</(script|iframe|frame|frameset|object|embed|applet|form|noscript)\s*>")
_FORM_TAGS = re.compile(r"(?is)</?(form|input|button|select|textarea)\b[^>]*>")
_HANDLER = re.compile(r"""(?is)\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""")
_SCRIPT_URL = re.compile(r"""(?is)(href|src|action|background|xlink:href)\s*=\s*(["']?)\s*(?:javascript|vbscript|data:text/html)[^"'\s>]*\2""")
_BODY = re.compile(r"(?is)<body\b[^>]*>(.*?)</body\s*>")
_STYLE = re.compile(r"(?is)<style\b[^>]*>.*?</style\s*>")
_HEAD = re.compile(r"(?is)<head\b[^>]*>.*?</head\s*>")
_OUTER = re.compile(r"(?is)</?(html|body|!doctype)\b[^>]*>")
_LINK = re.compile(r"(https?://[^\s<>\"']+)")

_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<base target="_blank">
<style>
html,body{{margin:0;padding:0;background:#fff;}}
body{{padding:18px 20px;font:15px/1.6 "Segoe UI",system-ui,-apple-system,Arial,sans-serif;color:#1f2328;word-wrap:break-word;overflow-wrap:anywhere;}}
img{{max-width:100%;height:auto;}}
table{{max-width:100%;}}
pre{{white-space:pre-wrap;}}
blockquote{{margin:0 0 0 12px;padding-left:12px;border-left:3px solid #d8dee4;color:#57606a;}}
a{{color:#0b57d0;}}
.plain{{white-space:pre-wrap;font-family:inherit;}}
</style>
{styles}
</head><body>{body}</body></html>"""


def clean_html(value: str) -> str:
    text = value or ""
    for pattern in (_BLOCKS, _LONE, _CLOSERS, _FORM_TAGS):
        text = pattern.sub("", text)
    text = _HANDLER.sub("", text)
    text = _SCRIPT_URL.sub(r'\1="#"', text)
    return text


def document(rich: str, plain: str = "") -> str:
    """A complete page for the reading frame: the letter's own HTML if it has one, else its text."""
    if (rich or "").strip():
        cleaned = clean_html(rich)
        styles = "\n".join(_STYLE.findall(cleaned))
        inner = _BODY.search(cleaned)
        body = inner.group(1) if inner else _OUTER.sub("", _HEAD.sub("", cleaned))
        if not inner:
            body = _STYLE.sub("", body)
        return _SHELL.format(styles=styles, body=body)
    text = (plain or "").strip() or "(This letter has no text.)"
    escaped = _LINK.sub(r'<a href="\1" rel="noopener noreferrer">\1</a>', html.escape(text, quote=False))
    return _SHELL.format(styles="", body=f'<div class="plain">{escaped}</div>')

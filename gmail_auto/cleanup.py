"""Archive inbox clutter that matches the owner's cleanup rules."""

from gmail_auto.errors import ConfigError
from gmail_auto.filters import Skip, classify
from gmail_auto.models import Mail
from gmail_auto.settings import load_settings

LIMIT = 40
_SKIP_CODES = {"system", "promotions", "social", "list", "forums", "auto"}

DEFAULT_MATCH = [
    "from:jobalerts-noreply@linkedin.com",
    "from:jobs-noreply@linkedin.com",
    "from:newsletters-noreply@linkedin.com",
    "from:jobs-recommendations@mail.trademe.co.nz",
    "from:linkedin.com",
    "from:workablemail.com",
    "job alert",
    "you may be a fit",
    "your application was sent",
]
DEFAULT_KEEP = [
    "from:etap.co.nz",
    "from:school.nz",
    "westlake",
    "wairau",
    "kiwiknowledge",
    "innopro",
]


def should_clean(mail: Mail, user_email: str, settings: dict | None = None) -> bool:
    settings = settings or load_settings()
    if _matches(mail, settings.get("cleanup_keep") or []):
        return False
    if _matches(mail, settings.get("cleanup_match") or []):
        return True
    if not settings.get("cleanup_skipped"):
        return False
    decision = classify(mail, user_email)
    return isinstance(decision, Skip) and decision.code in _SKIP_CODES


def run(client, settings: dict | None = None, limit: int = LIMIT) -> dict:
    """Archive matching letters from one mailbox. School and keep-list mail are left."""
    settings = settings or load_settings()
    user = client.get_user_email()
    scanned = 0
    archived = 0
    kept = 0
    try:
        mails = client.list_recent(limit, full=False)
    except TypeError:
        mails = client.list_recent(limit)
    for mail in mails:
        scanned += 1
        if not should_clean(mail, user, settings):
            continue
        if _matches(mail, settings.get("cleanup_keep") or []):
            kept += 1
            continue
        client.archive(mail.id)
        archived += 1
    return {"scanned": scanned, "archived": archived, "kept": kept, "account": user}


def run_all(clients, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    totals = {"scanned": 0, "archived": 0, "kept": 0, "accounts": []}
    errors = []
    for client in clients:
        try:
            result = run(client, settings)
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        for key in ("scanned", "archived", "kept"):
            totals[key] += result[key]
        if result.get("account"):
            totals["accounts"].append(result["account"])
    totals["errors"] = errors
    return totals


def _matches(mail: Mail, rules: list[str]) -> bool:
    sender = f"{mail.from_email or ''} {mail.from_name or ''}".lower()
    subject = (mail.subject or "").lower()
    snippet = (mail.snippet or mail.body or "").lower()
    blob = f"{sender} {subject} {snippet}"
    for rule in rules:
        text = (rule or "").strip().lower()
        if not text:
            continue
        if text.startswith("from:"):
            needle = text[5:].strip()
            if needle and needle in sender:
                return True
            continue
        if text.startswith("subject:"):
            needle = text[8:].strip()
            if needle and needle in subject:
                return True
            continue
        if text in blob:
            return True
    return False

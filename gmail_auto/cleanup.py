"""Archive inbox clutter that matches the owner's cleanup rules."""

from gmail_auto.errors import ConfigError
from gmail_auto.filters import Skip, classify
from gmail_auto.models import Mail
from gmail_auto.settings import load_settings

LIMIT = 300
_SKIP_CODES = {"system", "promotions", "social", "list", "forums", "auto"}

DEFAULT_MATCH = [
    "from:linkedin.com",
    "from:facebookmail.com",
    "from:facebook.com",
    "from:instagram.com",
    "from:meta.com",
    "from:trademe.co.nz",
    "from:doordash.com",
    "from:alibaba.com",
    "from:aliexpress.com",
    "from:heygen.com",
    "from:shopify.com",
    "from:moomoo.com",
    "from:futunn.com",
    "from:thetip.ai",
    "from:caffeine.ai",
    "from:indeed.com",
    "from:seek.co.nz",
    "from:seek.com.au",
    "from:workablemail.com",
    "from:greenhouse.io",
    "job alert",
    "job alerts",
    "you may be a fit",
    "your application was sent",
    "latest listings",
    "daily shot",
    "opportunities for you",
    "opportunities for y",
    "is hiring for",
    "dashpass",
]
DEFAULT_KEEP = [
    "from:etap.co.nz",
    "from:school.nz",
    "westlake",
    "wairau",
    "kiwiknowledge",
    "innopro",
    "from:accounts.google.com",
    "security alert",
    "verification code",
    "your code to confirm",
    "is your code",
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


def gmail_queries(settings: dict) -> list[str]:
    """Gmail search strings that find matching inbox clutter without opening each letter."""
    keep = [_gmail_negative(rule) for rule in settings.get("cleanup_keep") or []]
    keep = [item for item in keep if item]
    clauses = [_gmail_clause(rule) for rule in settings.get("cleanup_match") or []]
    clauses = [item for item in clauses if item]
    if settings.get("cleanup_skipped"):
        clauses.extend(["category:promotions", "category:social", "category:forums"])
    queries = []
    for start in range(0, len(clauses), 8):
        group = " OR ".join(clauses[start : start + 8])
        query = f"in:inbox -in:spam -in:trash ({group})"
        if keep:
            query += " " + " ".join(keep)
        queries.append(query)
    return queries


def run(client, settings: dict | None = None, limit: int = LIMIT) -> dict:
    """Archive matching letters from one mailbox. School and keep-list mail are left."""
    settings = settings or load_settings()
    user = client.get_user_email()
    if getattr(client, "provider", "") == "gmail" and hasattr(client, "list_ids"):
        return _run_gmail_search(client, settings, user, limit)
    scanned = 0
    archived = 0
    kept = 0
    pending = []
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
        pending.append(mail.id)
        archived += 1
    if pending:
        if hasattr(client, "archive_many"):
            client.archive_many(pending)
        else:
            for message_id in pending:
                client.archive(message_id)
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


def _run_gmail_search(client, settings: dict, user: str, limit: int) -> dict:
    seen: set[str] = set()
    pending = []
    for query in gmail_queries(settings):
        for message_id in client.list_ids(query, limit):
            if message_id in seen:
                continue
            seen.add(message_id)
            pending.append(message_id)
    if pending:
        client.archive_many(pending)
    return {"scanned": len(seen), "archived": len(pending), "kept": 0, "account": user, "provider": "gmail"}


def _gmail_clause(rule: str) -> str:
    text = (rule or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("from:"):
        needle = text[5:].strip()
        return f"from:{needle}" if needle else ""
    if lowered.startswith("subject:"):
        needle = text[8:].strip()
        return f'subject:"{needle}"' if needle else ""
    return f'"{text}"'


def _gmail_negative(rule: str) -> str:
    clause = _gmail_clause(rule)
    return f"-{clause}" if clause else ""


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

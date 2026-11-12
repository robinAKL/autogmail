import re
from dataclasses import dataclass

from gmail_auto.models import Mail

_PREFIXES = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "notification",
)
_EXACT = {
    "postmaster",
    "bounce",
    "bounces",
    "daemon",
    "mailer",
    "nobody",
    "null",
    "automated",
    "automailer",
}
_AUTO_SUBJECTS = (
    "automatic reply",
    "auto-reply",
    "auto reply",
    "autoreply",
    "out of office",
    "out-of-office",
    "自动回复",
    "外出自动回复",
    "休假自动回复",
)
_SENSITIVE = re.compile(
    "|".join(
        [
            r"passwords?",
            r"passcodes?",
            r"one[-\s]?time\s+(?:passwords?|codes?|pins?)",
            r"verification\s+codes?",
            r"verify(?:\s+your)?\s+codes?",
            r"security\s+codes?",
            r"\botp\b",
            r"\bcvv\b",
            r"\bcvc\b",
            r"social\s+security",
            r"bank\s+accounts?",
            r"credit\s+cards?",
            r"debit\s+cards?",
            r"routing\s+numbers?",
            r"密码",
            r"验证码",
            r"校验码",
            r"动态码",
            r"身份证",
            r"护照号",
            r"证件",
            r"银行卡",
            r"信用卡",
            r"借记卡",
            r"安全码",
            r"一次性密码",
        ]
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Skip:
    reason: str
    sensitive: bool = False
    code: str = "other"


@dataclass(frozen=True)
class Allow:
    language: str


def classify(mail: Mail, user_email: str) -> Skip | Allow:
    if is_sensitive(mail.subject) or is_sensitive(mail.body):
        return Skip("mentions passwords, codes, ID or bank details", sensitive=True, code="sensitive")
    if not mail.from_email:
        return Skip("no sender")
    reply_to = mail.reply_email or mail.from_email
    if same_mailbox(mail.from_email, user_email) or same_mailbox(reply_to, user_email):
        return Skip("from yourself", code="self")
    if is_system_address(mail.from_email) or is_system_address(reply_to):
        return Skip("system or no-reply address", code="system")
    headers = {key.lower(): value for key, value in mail.headers.items()}
    if _is_auto(headers, mail.subject):
        return Skip("automatic reply", code="auto")
    if _is_list(headers):
        return Skip("mailing list", code="list")
    labels = set(mail.label_ids)
    if "SPAM" in labels or "TRASH" in labels:
        return Skip("spam or deleted")
    if "DRAFT" in labels:
        return Skip("draft")
    if "CATEGORY_PROMOTIONS" in labels:
        return Skip("promotion", code="promotions")
    if "CATEGORY_SOCIAL" in labels:
        return Skip("social notification", code="social")
    if "CATEGORY_FORUMS" in labels:
        return Skip("forum mail", code="forums")
    if not mail.body.strip():
        return Skip("no text to answer", code="empty")
    return Allow(detect_language(f"{mail.subject}\n{mail.body}"))


def detect_language(text: str) -> str:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    if cjk == 0:
        return "en"
    if latin == 0 or cjk >= latin:
        return "zh"
    if cjk >= 8 and (cjk / (cjk + latin)) >= 0.2:
        return "zh"
    return "en"


def same_mailbox(left: str, right: str) -> bool:
    left_key = mailbox_key(left)
    right_key = mailbox_key(right)
    return left_key is not None and left_key == right_key


def mailbox_key(address: str) -> tuple[str, str] | None:
    text = (address or "").strip().lower()
    if "@" not in text:
        return None
    local, domain = text.split("@", 1)
    local = local.split("+", 1)[0]
    if domain == "googlemail.com":
        domain = "gmail.com"
    if not local or "." not in domain:
        return None
    return local, domain


def is_system_address(address: str) -> bool:
    key = mailbox_key(address)
    if key is None:
        return False
    local = key[0]
    if local in _EXACT:
        return True
    if any(local.startswith(prefix) for prefix in _PREFIXES):
        return True
    # Senders such as jobalerts-noreply@ or alerts.donotreply@ put the marker at the end.
    squashed = re.sub(r"[^a-z]", "", local)
    return "noreply" in squashed or "donotreply" in squashed


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(text or ""))


def reply_subject(subject: str) -> str:
    text = re.sub(r"\s+", " ", (subject or "").strip())
    if not text:
        text = "(no subject)"
    if re.match(r"(?i)^re\s*[:：]\s*", text):
        return text
    return f"Re: {text}"


def short_subject(subject: str) -> str:
    text = re.sub(r"\s+", " ", (subject or "").strip())
    if not text:
        return "(no subject)"
    if len(text) > 60:
        return text[:60] + "…"
    return text


def _is_auto(headers: dict[str, str], subject: str) -> bool:
    submitted = (headers.get("auto-submitted") or "").strip().lower()
    if submitted and submitted != "no":
        return True
    for key in ("x-autoreply", "x-autorespond", "x-autoresponder"):
        if headers.get(key):
            return True
    precedence = _tokens(headers.get("precedence", ""))
    if "auto_reply" in precedence or "auto-reply" in precedence:
        return True
    folded = re.sub(r"^(?:re\s*[:：]\s*)+", "", subject.lower()).strip()
    folded = folded.lstrip("【[(")
    return any(folded.startswith(item) for item in _AUTO_SUBJECTS)


def _is_list(headers: dict[str, str]) -> bool:
    if headers.get("list-id") or headers.get("mailing-list") or headers.get("x-mailing-list"):
        return True
    precedence = _tokens(headers.get("precedence", ""))
    return bool({"list", "bulk", "junk"} & set(precedence))


def _tokens(value: str) -> list[str]:
    return [item for item in re.split(r"[\s,;]+", value.lower()) if item]

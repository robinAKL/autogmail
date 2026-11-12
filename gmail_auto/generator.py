import json
import os
import re

from gmail_auto.errors import ReplyError
from gmail_auto.llm import complete, parse_json
from gmail_auto.models import Draft, Mail
from gmail_auto.profile_store import profile_as_prompt

_SYSTEM = """You answer one person's email as that person. You are not an assistant and you never mention being a model.

How to write:
- First person only, as if the owner typed it.
- Answer the specific thing the letter asks before anything else. No empty pleasantries.
- Use only facts stated in the profile, the knowledge base briefing, the owner's documents and the letter itself.
  Never invent experience, titles, phone numbers, addresses, prices, dates, schedules or promises.
- When the material does not cover something, say it cannot be confirmed yet, or decline in line with "Things I cannot promise".
- Follow the owner's rules exactly. They override everything else here.
- Keep close to the owner's tone. Use their typical phrases only where they fit naturally.
- Do not quote the original letter at length, do not write a subject line, do not explain what you are doing.

Decide one action:
- "reply": a routine letter you can answer completely from the material, with no new commitment.
- "ask": the owner must confirm before this is sent. Choose it when the reply would commit to a date, time, meeting,
  price, payment, delivery or decision; when a fact you need is missing; when a rule says to ask; when the subject is
  sensitive (legal, contract, complaint, HR, money, health); or whenever you are not sure. Still write your best draft.
- "skip": the letter needs no answer (a notification, an FYI, a thank-you with nothing to add).

Return JSON only, no other text:
{"action": "reply" | "ask" | "skip", "reason": "one short sentence in English", "summary": "one sentence in English saying what the sender wants", "reply": "the reply body, or empty for skip"}"""

_REVISE_SYSTEM = """You are revising a draft email reply written on behalf of its owner, following the owner's instruction.
Keep it first person, in the owner's voice. Use only facts from the profile, the knowledge base, the original letter and the owner's instruction.
The owner's instruction is authoritative: facts and commitments the owner gives you there may be used.
Return only the revised reply body, with no subject line and no explanation."""

_LANGUAGE_LINES = {
    "en": "Write the reply in English only, whatever language the letter is in.",
    "zh": "Write the reply in Chinese only.",
    "bilingual": "Write the reply in two parts: first Chinese, then a blank line, then the same message in English.",
}
_AI = re.compile(
    r"(作为\s*(?:一个\s*)?(?:AI|人工智能)|我是\s*(?:一个\s*)?(?:AI|语言模型|人工智能)|"
    r"as an ai|i am an ai|i'm an ai|language model)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_ACTIONS = {"reply", "ask", "skip"}


def reply_language(setting: str, detected: str) -> str:
    """The language a reply must be in, from the setting and the letter's own language."""
    if setting == "bilingual":
        return "bilingual"
    if setting == "match":
        return "zh" if detected == "zh" else "en"
    return "en"


def draft_reply(mail: Mail, profile: dict, detected: str) -> Draft:
    """Draft a reply from the profile and the knowledge base, and decide whether it may go out unasked."""
    from gmail_auto.knowledge import context_for, render_context
    from gmail_auto.settings import load_settings

    language = reply_language(load_settings()["reply_language"], detected)
    context = context_for(mail)
    knowledge = render_context(context)
    raw = complete(_SYSTEM, _user_prompt(mail, profile, knowledge, language))
    data = parse_json(raw)
    if data is None:
        # No structure came back. Keep the text as a draft, but never send it without the owner seeing it.
        draft = Draft(action="ask", reply=clean_reply(raw), reason="The draft needs a look before sending.")
    else:
        action = str(data.get("action") or "").strip().lower()
        draft = Draft(
            action=action if action in _ACTIONS else "ask",
            reply=clean_reply(str(data.get("reply") or "")),
            reason=_line(data.get("reason")),
            summary=_line(data.get("summary")),
        )
    draft.sources = context["sources"]
    if draft.action == "skip":
        draft.reply = ""
        return draft
    ensure_safe(draft.reply, profile, language, mail, knowledge)
    return draft


def revise_reply(item: dict, instruction: str, profile: dict) -> str:
    """Rewrite a waiting draft the way the owner asked for over WhatsApp or the web page."""
    from gmail_auto.knowledge import load_index
    from gmail_auto.settings import load_settings

    index = load_index()
    language = reply_language(load_settings()["reply_language"], item.get("language") or "en")
    prompt = (
        "PROFILE:\n" + profile_as_prompt(profile) + "\n\n"
        + ("KNOWLEDGE BASE BRIEFING:\n" + index["brief"]["text"] + "\n\n" if index["brief"]["text"] else "")
        + f"ORIGINAL LETTER from {item.get('from_name') or item.get('from_email')}:\n"
        f"Subject: {item.get('subject') or '(no subject)'}\n{_clip(item.get('body', ''), 4000)}\n\n"
        f"CURRENT DRAFT:\n{item.get('draft', '')}\n\n"
        f"OWNER'S INSTRUCTION:\n{instruction.strip()}\n\n"
        f"{_LANGUAGE_LINES[language]}"
    )
    reply = clean_reply(complete(_REVISE_SYSTEM, prompt))
    check_basics(reply)
    return reply


def ensure_safe(reply: str, profile: dict, language: str, mail: Mail | None = None, knowledge: str = "") -> None:
    text = check_basics(reply)
    cjk = len(re.findall(r"[一-鿿]", text))
    english_words = len(re.findall(r"[A-Za-z]{2,}", text))
    if language == "bilingual" and (cjk < 2 or english_words < 3):
        raise ReplyError("The reply must contain both Chinese and English. It was held back.")
    if language == "zh" and cjk < 2:
        raise ReplyError("The reply was not in Chinese. It was held back.")
    if language == "en" and english_words < 3:
        raise ReplyError("The reply was not in English. It was held back.")
    if _unknown_number(text, profile, knowledge):
        raise ReplyError("The reply contains a number that is not in your material. It was held back.")
    context = _context(profile, mail, knowledge).lower()
    if any(url.lower().rstrip(".,;)") not in context for url in _URL.findall(text)):
        raise ReplyError("The reply contains a link that is not in your material. It was held back.")
    if any(addr.lower() not in context for addr in _EMAIL.findall(text)):
        raise ReplyError("The reply contains an email address that is not in your material. It was held back.")


def check_basics(reply: str) -> str:
    text = (reply or "").strip()
    if not text:
        raise ReplyError("The model returned an empty reply.")
    if len(text) > 5000:
        raise ReplyError("The reply was unusually long and was discarded.")
    key = os.getenv("OPENAI_API_KEY", "")
    if key and len(key) > 8 and key in text:
        raise ReplyError("The reply contained an API key and was discarded.")
    if re.search(r"sk-[A-Za-z0-9]{10,}", text):
        raise ReplyError("The reply contained what looks like an API key and was discarded.")
    if _AI.search(text):
        raise ReplyError("The reply did not read like you wrote it and was discarded.")
    return text


def clean_reply(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()
    cleaned = re.sub(r"(?i)^(subject|主题)\s*[:：]\s*.*(?:\n+|$)", "", cleaned, count=1)
    cleaned = re.sub(r"(?i)^(回复|reply)\s*[:：]\s*", "", cleaned, count=1)
    return cleaned.strip()


def _user_prompt(mail: Mail, profile: dict, knowledge: str, language: str) -> str:
    return (
        "MY PROFILE:\n"
        f"{profile_as_prompt(profile)}\n\n"
        + (knowledge + "\n\n" if knowledge else "")
        + "THE LETTER TO ANSWER:\n"
        f"From: {mail.from_name or mail.from_email}\n"
        f"Address: {mail.from_email}\n"
        f"Subject: {mail.subject or '(no subject)'}\n"
        "Body:\n"
        f"{_clip(mail.body)}\n\n"
        f"{_LANGUAGE_LINES[language]}\n"
        "Return the JSON object only."
    )


def _unknown_number(reply: str, profile: dict, knowledge: str = "") -> bool:
    allowed = re.sub(r"\D", "", json.dumps(profile, ensure_ascii=False) + knowledge)
    for digits in re.findall(r"\d{7,}", reply):
        if len(digits) == 8 and digits.startswith(("19", "20")):
            continue
        if digits not in allowed:
            return True
    for match in re.finditer(r"\d{3,4}[\s\-]\d{3,4}[\s\-]\d{3,4}", reply):
        digits = re.sub(r"\D", "", match.group())
        if digits and digits not in allowed:
            return True
    return False


def _context(profile: dict, mail: Mail | None, knowledge: str = "") -> str:
    parts = [json.dumps(profile, ensure_ascii=False), knowledge]
    if mail is not None:
        parts.extend([mail.from_email, mail.reply_email, mail.subject, mail.body])
    return "\n".join(parts)


def _line(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:300]


def _clip(text: str, limit: int = 6000) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "\n(the letter is cut here)"

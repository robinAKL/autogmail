"""Seamless multi-language translation engine for inbound reading and outbound replies."""

import html
import re
import logging
from typing import Tuple, List, Dict

from gmail_auto.errors import ConfigError
from gmail_auto.llm import complete, model_ready
from gmail_auto.models import Mail

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES: Dict[str, str] = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "ru": "Russian",
    "ar": "Arabic",
    "vi": "Vietnamese",
    "th": "Thai",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "id": "Indonesian",
    "ms": "Malay",
    "hi": "Hindi",
}

# Regex for detecting common quote headers in emails
_QUOTE_PATTERNS = [
    re.compile(r"^\s*On\s+.+?wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*-+\s*Original Message\s*-+\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*在\s*.+?\s*写道：\s*$", re.MULTILINE),
    re.compile(r"^\s*_{10,}\s*$", re.MULTILINE),
    re.compile(r"^\s*From:\s+.+?Sent:\s+.+?$", re.MULTILINE | re.IGNORECASE),
]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_AMOUNT_RE = re.compile(r"(?:[\$€£¥元]|NZD|USD|EUR|GBP|AUD|RMB|CAD)\s*[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:dollars?|yuan|cents?)", re.IGNORECASE)
_CURRENCY_CUE = re.compile(r"[\$€£¥元]|NZD|USD|EUR|GBP|AUD|RMB|CAD", re.IGNORECASE)


def detect_language(text: str) -> str:
    """Detect language of the text using character blocks and linguistic features.
    
    Returns standard language code (e.g. 'zh', 'en', 'ja', 'ko', 'ru', etc.).
    """
    if not text or not text.strip():
        return "en"

    # 1. Non-Latin scripts have distinct Unicode blocks
    hangul = len(re.findall(r"[\uac00-\ud7af\u1100-\u11ff]", text))
    if hangul >= 3:
        return "ko"

    hiragana_katakana = len(re.findall(r"[\u3040-\u30ff]", text))
    if hiragana_katakana >= 3:
        return "ja"

    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    if cjk >= 3:
        # If there are kanji but also kana, Japanese was caught above. Otherwise Chinese.
        return "zh"

    cyrillic = len(re.findall(r"[\u0400-\u04ff]", text))
    if cyrillic >= 4:
        return "ru"

    arabic = len(re.findall(r"[\u0600-\u06ff]", text))
    if arabic >= 4:
        return "ar"

    thai = len(re.findall(r"[\u0e00-\u0e7f]", text))
    if thai >= 4:
        return "th"

    devanagari = len(re.findall(r"[\u0900-\u097f]", text))
    if devanagari >= 4:
        return "hi"

    vietnamese_accents = len(re.findall(r"[đơưảãạắằẳẵặấầẩẫậéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]", text, re.IGNORECASE))
    if vietnamese_accents >= 4:
        return "vi"

    # 2. Latin script language hints
    lower = text.lower()
    
    # German specific characters or words
    if re.search(r"[äöüß]", lower) or re.search(r"\b(und|nicht|ich|sie|das|ist|der|die|mit|f[uü]r)\b", lower):
        german_hits = len(re.findall(r"\b(und|nicht|ich|sie|das|ist|der|die|mit|f[uü]r|bitte|danke|sehr)\b", lower))
        if german_hits >= 2:
            return "de"

    # French specific characters or words
    if re.search(r"[éèêàçùœ]", lower) or re.search(r"\b(le|la|les|des|un|une|vous|nous|pour|est|dans)\b", lower):
        french_hits = len(re.findall(r"\b(bonjour|merci|vous|nous|pour|est|dans|avec|cette|votre)\b", lower))
        if french_hits >= 2:
            return "fr"

    # Spanish
    if re.search(r"[ñ¿¡áíóú]", lower) or re.search(r"\b(el|la|los|las|por|para|con|una|est[aá]|gracias)\b", lower):
        spanish_hits = len(re.findall(r"\b(hola|gracias|por|para|con|una|buenos|estimado|saludos)\b", lower))
        if spanish_hits >= 2:
            return "es"

    # Turkish (unique chars ğ, ş, ı)
    if len(re.findall(r"[ğşış]", lower)) >= 2 or len(re.findall(r"\b(merhaba|teşekkür|teşekkürler|için|lütfen|ile|saygılarımla)\b", lower)) >= 2:
        return "tr"

    # Polish (unique chars ą, ę, ł, ń, ś, ź, ż)
    if len(re.findall(r"[ąćęłńśźż]", lower)) >= 2 or len(re.findall(r"\b(proszę|dziękuję|witam|pozdrawiam|jest|nie|tak)\b", lower)) >= 2:
        return "pl"

    # Portuguese (ã, õ or strong keyword hits)
    if len(re.findall(r"[ãõ]", lower)) >= 1 and len(re.findall(r"\b(olá|obrigado|obrigada|para|com|não|você|bom|prezado|atenciosamente)\b", lower)) >= 1:
        return "pt"
    if len(re.findall(r"\b(olá|obrigado|obrigada|você|prezado|atenciosamente)\b", lower)) >= 2:
        return "pt"

    # Italian
    if len(re.findall(r"\b(ciao|grazie|buongiorno|gentile|cordiali|saluti|sono|per|con|che)\b", lower)) >= 2:
        if re.search(r"\b(ciao|grazie|buongiorno|gentile|cordiali|saluti)\b", lower):
            return "it"

    # Dutch
    if len(re.findall(r"\b(hallo|bedankt|groeten|voor|met|het|een|van|niet|met\s+vriendelijke)\b", lower)) >= 2:
        if re.search(r"\b(hallo|bedankt|groeten|vriendelijke)\b", lower):
            return "nl"

    # Indonesian / Malay
    if len(re.findall(r"\b(terima\s+kasih|selamat|dengan|untuk|yang|tidak|mohon|silakan|sila|semak|encik|puan|tuan)\b", lower)) >= 2:
        if re.search(r"\b(sila|semak|encik|puan|tuan)\b", lower):
            return "ms"
        return "id"

    return "en"


def _quote_label(user_lang: str) -> str:
    """The line between the translated message and the untranslated history, in the reader's language."""
    from gmail_auto.i18n import text

    return text("--- Quoted message ---", user_lang)


def split_mail_body(body: str) -> Tuple[str, str]:
    """Split email body into (active_message, quoted_history).
    
    Only the active message needs to be translated, which saves tokens and prevents timeouts.
    """
    if not body:
        return "", ""

    earliest_pos = -1
    for pat in _QUOTE_PATTERNS:
        m = pat.search(body)
        if m:
            if earliest_pos == -1 or m.start() < earliest_pos:
                earliest_pos = m.start()

    if earliest_pos != -1:
        return body[:earliest_pos].strip(), body[earliest_pos:].strip()
    return body.strip(), ""


def translate_text(text: str, from_lang: str, to_lang: str, context: str = "", *, required: bool = False) -> str:
    """Translate text from from_lang to to_lang with entity protection and business tone."""
    if not text or not text.strip():
        return text
    if from_lang and to_lang and from_lang.lower() == to_lang.lower():
        return text
    if not model_ready():
        if required:
            raise ConfigError("Set up a writing model before translating a reply.")
        return text

    target_name = SUPPORTED_LANGUAGES.get(to_lang.lower(), to_lang)
    source_name = SUPPORTED_LANGUAGES.get(from_lang.lower(), from_lang)

    system_prompt = (
        f"You are a professional business email translator. "
        f"Translate the following email text from {source_name} to {target_name}.\n\n"
        "STRICT RULES:\n"
        "1. Translate naturally, professionally, and fluently in a business email tone.\n"
        "2. DO NOT translate or alter: email addresses, URLs/hyperlinks, phone numbers, "
        "amounts/currencies (e.g. $50, €100), dates, times, and order/tracking numbers.\n"
        "3. Preserve the original paragraph layout, bullet points, and signatures.\n"
        "4. Output ONLY the translated text without adding any explanations, markdown code fences, or disclaimers."
    )

    user_prompt = ""
    if context:
        user_prompt += f"[Email Context: {context}]\n\n"
    user_prompt += f"[Text to Translate]:\n{text}"

    try:
        translated = complete(system_prompt, user_prompt, max_tokens=1500, temperature=0.2, role="worker")
    except Exception as exc:
        if required:
            raise ConfigError("The reply could not be translated. Please try again.") from exc
        logger.warning(f"Translation failed: {exc}")
        return text
    translated = (translated or "").strip()
    if required and not translated:
        raise ConfigError("The reply translation was empty. Please try again.")
    return translated or text


_HTML_BLOCK_RE = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")
_HTML_BREAK_RE = re.compile(r"(?is)<(?:br\s*/?|/(?:p|div|li|tr|h[1-6]))>")
_HTML_TAG_RE = re.compile(r"(?is)<[^>]+>")


def _html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = _HTML_BLOCK_RE.sub("", raw_html)
    text = _HTML_BREAK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def translate_inbound_mail(mail: Mail, user_lang: str = "zh", preserve_langs: List[str] | None = None) -> Mail:
    """Translate an incoming mail to user's first language if needed.
    
    Populates detected_lang, translated_subject, translated_body, translated_snippet.
    """
    source_body = (mail.body or "").strip() or _html_to_text(getattr(mail, "html", ""))
    full_sample = f"{mail.subject or ''}\n{source_body}"
    detected = detect_language(full_sample)
    mail.detected_lang = detected

    preserve = [p.lower() for p in (preserve_langs or [])]
    # If the email is already in the user's first language or in preserve_langs, no translation needed
    if detected.lower() == user_lang.lower() or detected.lower() in preserve:
        mail.translated_subject = mail.subject
        mail.translated_body = mail.body
        mail.translated_snippet = mail.snippet
        return mail

    context = f"Sender: {mail.from_name} <{mail.from_email}>, Subject: {mail.subject}"

    # 1. Translate subject
    if mail.subject:
        mail.translated_subject = translate_text(mail.subject, detected, user_lang, context=context)
    else:
        mail.translated_subject = ""

    # 2. Translate body (separating active text from quoted history to save tokens)
    active_body, quoted_history = split_mail_body(source_body)
    if active_body:
        translated_active = translate_text(active_body, detected, user_lang, context=context)
        if quoted_history:
            mail.translated_body = f"{translated_active}\n\n{_quote_label(user_lang)}\n{quoted_history}"
        else:
            mail.translated_body = translated_active
    else:
        mail.translated_body = mail.body

    # 3. Create snippet
    if mail.translated_body:
        mail.translated_snippet = mail.translated_body[:150].replace("\n", " ").strip()
    else:
        mail.translated_snippet = mail.snippet

    return mail


def translate_outbound_reply(reply_text: str, target_lang: str, user_lang: str = "zh") -> str:
    """Translate a user-composed L1 reply to recipient's language."""
    if not reply_text or not reply_text.strip():
        return reply_text
    if target_lang.lower() == user_lang.lower():
        return reply_text

    translated = translate_text(reply_text, user_lang, target_lang, required=True)
    preserved, _ = verify_entities_preserved(reply_text, translated)
    if not preserved:
        raise ConfigError("The translation changed an email address, link, or amount. Check the reply and try again.")
    return translated


def extract_entities(text: str) -> Dict[str, List[str]]:
    """Extract key business entities for safety alignment checking."""
    urls = [u.rstrip(".,;:!?。，；：！？)]}") for u in _URL_RE.findall(text or "")]
    return {
        "emails": _EMAIL_RE.findall(text or ""),
        "urls": [u for u in urls if u],
        "amounts": _AMOUNT_RE.findall(text or ""),
    }


def verify_entities_preserved(src: str, dst: str) -> Tuple[bool, List[str]]:
    """Verify that critical entities in source text were not altered or omitted in destination text.
    
    Returns (is_valid, list_of_missing_or_mismatched_entities).
    """
    src_entities = extract_entities(src)
    dst_lower = (dst or "").lower()
    missing = []

    # Verify emails
    for email in src_entities["emails"]:
        if email.lower() not in dst_lower:
            missing.append(f"Email missing: {email}")

    # Verify URLs
    for url in src_entities["urls"]:
        # URLs must be preserved exactly
        if url not in dst:
            missing.append(f"URL missing or altered: {url}")

    # Digits and the currency marker both have to survive. "500" alone is not "$500".
    for amt in src_entities["amounts"]:
        digits = re.findall(r"\d+", amt)
        for d in digits:
            if d not in dst:
                missing.append(f"Amount/figure missing: {d} (from {amt})")
        for cue in _CURRENCY_CUE.findall(amt):
            if cue.lower() not in dst_lower and cue not in dst:
                missing.append(f"Currency missing: {cue} (from {amt})")

    return (len(missing) == 0, missing)

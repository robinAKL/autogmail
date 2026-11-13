"""The knowledge base replies are written from.

It holds documents (uploaded files, notes, summaries of past email, SharePoint
files), the user's own reply rules, and one written "understanding" of all of
it. Every reply is drafted from the profile plus what is found here.
"""

import math
import re
import secrets
import time
from datetime import datetime

from gmail_auto.envfile import has_openai_key
from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.filters import is_sensitive
from gmail_auto.llm import complete
from gmail_auto.profile_store import profile_as_prompt
from gmail_auto.storage import LOCK, delete_doc, load_doc, save_doc

SOURCES = {
    "upload": "Uploaded file",
    "note": "Note",
    "email": "Email history",
    "sharepoint": "SharePoint",
}
MAX_DOC_CHARS = 300_000
MAX_DOCS = 200
MAX_RULES = 40
_CHUNK = 1100
_OVERLAP = 150
_SECRET = re.compile(r"sk-[A-Za-z0-9]{10,}")
_STOP = set(
    "the a an and or of to in on for with is are was were be been it this that these those as at by from "
    "i you he she we they my your our their me us them do does did not no yes can could will would should "
    "have has had if then so but about into over under up down out just than too very re fw fwd hi hello "
    "dear regards thanks thank please".split()
)
_cache: dict = {"stamp": None, "chunks": []}

_BRIEF_SYSTEM = """You maintain a private briefing that an email assistant reads before answering mail on behalf of one person.
Write it from the material given. Use only what the material states. Never invent facts, names, numbers or dates.
Write in English, in plain short sentences, using exactly these markdown sections:

## Who I am
## Key facts
## People and organisations
## Ongoing topics and commitments
## How I write
## How to handle my mail

Under each heading use short bullet points starting with "- ". If the material says nothing for a section, write "- Nothing recorded yet."
Under "How to handle my mail" restate the user's own rules first, then add cautions that follow from the material.
Keep the whole briefing under 500 words."""

_SUMMARY_SYSTEM = """Summarise this document for a private knowledge base used to answer email on the owner's behalf.
Give 3 to 7 short bullet points starting with "- " that capture the facts, names, dates, numbers and decisions someone would need when replying to related email.
Use only what the document says. Write in English."""

_HISTORY_SYSTEM = """You are reading a batch of one person's email so an assistant can later answer mail the way they would.
"SENT" letters were written by the owner. "RECEIVED" letters were written to the owner.
Extract only what the letters state. Never invent anything. Leave out passwords, codes, card or ID numbers.
Write in English using exactly these markdown sections, each with short "- " bullet points:

## People and organisations
(who they are to the owner, and what they usually write about)
## Ongoing topics and commitments
## Facts the owner stated about themself
## How the owner writes
(greeting, sign-off, length, tone, typical phrases - only from SENT letters)"""


# ---- documents ---------------------------------------------------------------


def load_index() -> dict:
    data = load_doc("kb_index", {})
    if not isinstance(data, dict):
        data = {}
    docs = [item for item in data.get("docs", []) if isinstance(item, dict) and item.get("id")]
    rules = [str(item) for item in data.get("rules", []) if isinstance(item, str) and item.strip()]
    brief = data.get("brief") if isinstance(data.get("brief"), dict) else {}
    return {
        "docs": docs,
        "rules": rules,
        "brief": {
            "text": str(brief.get("text") or ""),
            "updated_at": str(brief.get("updated_at") or ""),
            "stale": bool(brief.get("stale")),
        },
        "stamp": data.get("stamp") or 0,
    }


def list_documents() -> list[dict]:
    docs = load_index()["docs"]
    docs.sort(key=lambda item: item.get("added_at", ""), reverse=True)
    return docs


def get_document(doc_id: str) -> dict | None:
    for item in load_index()["docs"]:
        if item["id"] == doc_id:
            body = load_doc(_body_name(doc_id), {})
            text = body.get("text", "") if isinstance(body, dict) else ""
            return {**item, "text": text}
    return None


def add_document(title: str, text: str, source: str, origin: str = "", meta: dict | None = None, summarize: bool = True) -> dict:
    """Store a document. One with the same source and origin is replaced, so re-syncing does not duplicate."""
    if source not in SOURCES:
        raise ConfigError("Unknown knowledge source.")
    title = re.sub(r"\s+", " ", (title or "").strip())[:160] or "Untitled"
    text = _SECRET.sub("[removed key]", (text or "").strip())
    if not text:
        raise ConfigError(f"'{title}' has no text to keep.")
    clipped = len(text) > MAX_DOC_CHARS
    text = text[:MAX_DOC_CHARS]
    summary = summarize_document(title, text) if summarize else ""
    with LOCK:
        index = load_index()
        existing = None
        if origin:
            existing = next((item for item in index["docs"] if item.get("source") == source and item.get("origin") == origin), None)
        if existing is None and len(index["docs"]) >= MAX_DOCS:
            raise ConfigError(f"The knowledge base is full ({MAX_DOCS} documents). Remove some first.")
        doc_id = existing["id"] if existing else secrets.token_hex(6)
        entry = {
            "id": doc_id,
            "title": title,
            "source": source,
            "origin": origin,
            "added_at": _now(),
            "chars": len(text),
            "clipped": clipped,
            "summary": summary,
            "meta": meta or {},
        }
        save_doc(_body_name(doc_id), {"text": text})
        index["docs"] = [item for item in index["docs"] if item["id"] != doc_id] + [entry]
        index["brief"]["stale"] = True
        _save_index(index)
    return entry


def delete_document(doc_id: str) -> bool:
    with LOCK:
        index = load_index()
        kept = [item for item in index["docs"] if item["id"] != doc_id]
        if len(kept) == len(index["docs"]):
            return False
        index["docs"] = kept
        index["brief"]["stale"] = True
        _save_index(index)
        delete_doc(_body_name(doc_id))
    return True


def summarize_document(title: str, text: str) -> str:
    fallback = _first_lines(text)
    if not has_openai_key():
        return fallback
    try:
        result = complete(_SUMMARY_SYSTEM, f"Title: {title}\n\n{_head(text, 12000)}", max_tokens=350, temperature=0.2)
    except ReplyError:
        return fallback
    return result.strip() or fallback


# ---- rules and understanding -------------------------------------------------


def save_rules(text_or_list) -> list[str]:
    if isinstance(text_or_list, str):
        items = [line.strip().lstrip("-•* ").strip() for line in text_or_list.splitlines()]
    else:
        items = [str(item).strip() for item in text_or_list]
    items = [item for item in items if item]
    if len(items) > MAX_RULES:
        raise ConfigError(f"Too many rules. Keep it to {MAX_RULES} or fewer.")
    for item in items:
        if len(item) > 300:
            raise ConfigError("One of the rules is too long. Keep each under 300 characters.")
    with LOCK:
        index = load_index()
        if items != index["rules"]:
            index["rules"] = items
            index["brief"]["stale"] = True
            _save_index(index)
    return items


def save_brief(text: str) -> None:
    text = (text or "").strip()
    if len(text) > 12000:
        raise ConfigError("The understanding is too long. Keep it under 12,000 characters.")
    with LOCK:
        index = load_index()
        index["brief"] = {"text": text, "updated_at": _now(), "stale": False}
        _save_index(index)


def rebuild_brief(profile: dict) -> str:
    """Ask the model to re-read everything and write the understanding again."""
    index = load_index()
    parts = ["PROFILE\n" + profile_as_prompt(profile)]
    if index["rules"]:
        parts.append("THE USER'S OWN RULES\n" + "\n".join(f"- {rule}" for rule in index["rules"]))
    budget = 24000
    for item in sorted(index["docs"], key=lambda doc: doc.get("added_at", ""), reverse=True):
        body = load_doc(_body_name(item["id"]), {})
        text = body.get("text", "") if isinstance(body, dict) else ""
        piece = text if len(text) <= 2500 else (item.get("summary") or _first_lines(text))
        block = f"DOCUMENT: {item['title']} ({SOURCES.get(item['source'], item['source'])})\n{piece}"
        if len(block) > budget:
            break
        budget -= len(block)
        parts.append(block)
    text = complete(_BRIEF_SYSTEM, "\n\n".join(parts), max_tokens=1100, temperature=0.2).strip()
    if not text:
        raise ReplyError("The model returned nothing.")
    save_brief(text)
    return text


def refresh_brief_if_stale(profile: dict) -> None:
    """Called before a round of mail, so replies never run on an out-of-date understanding."""
    index = load_index()
    if not index["brief"]["stale"] or not has_openai_key():
        return
    if not index["docs"] and not index["rules"] and not index["brief"]["text"]:
        return
    try:
        rebuild_brief(profile)
    except ConfigError:
        return


# ---- learning from past email ------------------------------------------------


def learn_from_mailbox(client, limit: int = 30) -> dict:
    """Read recent sent and received mail and keep a summary of what it shows."""
    account = client.get_user_email()
    letters = []
    for direction, mails in (("SENT", client.list_sent(limit, full=True)), ("RECEIVED", client.list_recent(limit, full=True))):
        for mail in mails:
            if is_sensitive(mail.subject) or is_sensitive(mail.body) or not (mail.body or "").strip():
                continue
            labels = set(mail.label_ids or [])
            if direction == "RECEIVED" and labels & {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM"}:
                continue
            other = mail.headers.get("to", "") if direction == "SENT" else (mail.from_name or mail.from_email)
            letters.append(
                f"[{direction}] {mail.date} | {'To' if direction == 'SENT' else 'From'}: {other}\n"
                f"Subject: {mail.subject or '(no subject)'}\n{_head(mail.body, 1200)}"
            )
    if not letters:
        raise ConfigError("No readable mail was found to learn from.")
    notes = []
    for start in range(0, len(letters), 10):
        batch = "\n\n-----\n\n".join(letters[start : start + 10])
        notes.append(complete(_HISTORY_SYSTEM, batch, max_tokens=900, temperature=0.2).strip())
    combined = notes[0]
    if len(notes) > 1:
        combined = complete(
            _HISTORY_SYSTEM + "\n\nYou are given several sets of notes already in this format. Merge them into one, removing repeats.",
            "\n\n=====\n\n".join(notes),
            max_tokens=1400,
            temperature=0.2,
        ).strip()
    if not combined:
        raise ReplyError("The model returned nothing.")
    provider = getattr(client, "provider", "gmail")
    return add_document(
        f"What your email shows ({account})",
        combined,
        "email",
        origin=f"{provider}:{account}",
        meta={"letters": len(letters), "account": account, "provider": provider},
        summarize=False,
    )


# ---- finding what is relevant to one letter ----------------------------------


def context_for(mail, limit: int = 4) -> dict:
    index = load_index()
    query = f"{mail.subject}\n{mail.from_name} {mail.from_email}\n{_head(mail.body, 3000)}"
    passages = search(query, limit)
    return {
        "brief": index["brief"]["text"],
        "rules": index["rules"],
        "passages": passages,
        "sources": _unique([item["title"] for item in passages]),
    }


def search(query: str, limit: int = 4) -> list[dict]:
    chunks = _chunks()
    if not chunks:
        return []
    terms = _tokens(query)
    if not terms:
        return []
    wanted = set(terms)
    total = len(chunks)
    frequency: dict[str, int] = {}
    for chunk in chunks:
        for term in wanted & chunk["set"]:
            frequency[term] = frequency.get(term, 0) + 1
    average = sum(chunk["length"] for chunk in chunks) / total
    scored = []
    for chunk in chunks:
        score = 0.0
        for term in wanted & chunk["set"]:
            idf = math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
            count = chunk["counts"][term]
            score += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * chunk["length"] / average))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda pair: -pair[0])
    if not scored:
        return []
    floor = scored[0][0] * 0.35
    return [
        {"title": chunk["title"], "text": chunk["text"], "doc_id": chunk["doc_id"], "score": round(score, 2)}
        for score, chunk in scored[:limit]
        if score >= floor
    ]


def render_context(context: dict) -> str:
    """The knowledge part of the prompt."""
    parts = []
    if context.get("brief"):
        parts.append("WHAT YOU KNOW ABOUT ME (my knowledge base briefing):\n" + context["brief"])
    if context.get("rules"):
        parts.append("MY RULES (always follow these):\n" + "\n".join(f"- {rule}" for rule in context["rules"]))
    for item in context.get("passages") or []:
        parts.append(f"FROM MY DOCUMENT \"{item['title']}\":\n{item['text']}")
    return "\n\n".join(parts)


def overview() -> dict:
    index = load_index()
    counts: dict[str, int] = {}
    for item in index["docs"]:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    return {
        "documents": len(index["docs"]),
        "by_source": counts,
        "rules": len(index["rules"]),
        "brief_ready": bool(index["brief"]["text"]),
        "brief_stale": index["brief"]["stale"],
        "characters": sum(int(item.get("chars", 0) or 0) for item in index["docs"]),
    }


# ---- internals ---------------------------------------------------------------


def _chunks() -> list[dict]:
    index = load_index()
    if _cache["stamp"] == index["stamp"] and _cache["stamp"]:
        return _cache["chunks"]
    chunks = []
    for item in index["docs"]:
        body = load_doc(_body_name(item["id"]), {})
        text = body.get("text", "") if isinstance(body, dict) else ""
        for piece in _split(text):
            tokens = _tokens(item["title"] + "\n" + piece)
            if not tokens:
                continue
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            chunks.append(
                {
                    "doc_id": item["id"],
                    "title": item["title"],
                    "text": piece,
                    "counts": counts,
                    "set": set(counts),
                    "length": len(tokens),
                }
            )
    _cache["stamp"] = index["stamp"]
    _cache["chunks"] = chunks
    return chunks


def _split(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= _CHUNK:
        return [text] if text else []
    pieces = []
    start = 0
    while start < len(text):
        end = min(len(text), start + _CHUNK)
        if end < len(text):
            for mark in ("\n\n", "\n", ". ", "。", " "):
                cut = text.rfind(mark, start + _CHUNK // 2, end)
                if cut != -1:
                    end = cut + len(mark)
                    break
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - _OVERLAP, start + 1)
    return pieces


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    words = [word for word in re.findall(r"[a-z0-9][a-z0-9'_-]{1,}", lowered) if word not in _STOP]
    cjk = re.findall(r"[一-鿿]+", lowered)
    pairs = []
    for run in cjk:
        if len(run) == 1:
            pairs.append(run)
        pairs.extend(run[i : i + 2] for i in range(len(run) - 1))
    return words + pairs


def _save_index(index: dict) -> None:
    index["stamp"] = time.time()
    save_doc("kb_index", {key: index[key] for key in ("docs", "rules", "brief", "stamp")})


def _body_name(doc_id: str) -> str:
    return f"kb_{re.sub(r'[^A-Za-z0-9]', '', doc_id)}"


def _head(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n(cut here)"


def _first_lines(text: str, limit: int = 320) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def _unique(items: list[str]) -> list[str]:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

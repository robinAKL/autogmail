"""The knowledge base replies are written from.

It holds documents (uploaded files, notes, summaries of past email, SharePoint
files), the user's own reply rules, and a structured reading of all of that
(facts, people, topics, experience, preferences, rules, decisions). Every reply
is drafted from the profile plus what is found here.
"""

import json
import math
import re
import secrets
import time
from datetime import datetime

from gmail_auto.envfile import has_openai_key
from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.filters import is_sensitive
from gmail_auto.llm import complete, parse_json
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

KINDS = {
    "fact": {
        "label": "Facts",
        "locked": True,
        "hint": "Stated in your documents or mail. Replies treat current facts as true. They cannot be rewritten. Mark them no longer current, or reorganise so a later source replaces them on the timeline.",
    },
    "person": {
        "label": "People",
        "locked": False,
        "hint": "Who you write with, and how they relate to you.",
    },
    "topic": {
        "label": "Topics",
        "locked": False,
        "hint": "Ongoing work, school, projects and threads the assistant should recognise.",
    },
    "experience": {
        "label": "Experience",
        "locked": False,
        "hint": "Patterns from how you have handled similar mail before.",
    },
    "preference": {
        "label": "Preferences",
        "locked": False,
        "hint": "Tone, length, sign-off and language.",
    },
    "rule": {
        "label": "Rules",
        "locked": False,
        "hint": "Always win over everything else. You can change or drop these.",
    },
    "decision": {
        "label": "Decisions",
        "locked": False,
        "hint": "Calls you have already made that later mail should not reopen.",
    },
}
_KIND_ORDER = tuple(KINDS)
_HEADING_KIND = {
    "who i am": "fact",
    "key facts": "fact",
    "people and organisations": "person",
    "people": "person",
    "ongoing topics and commitments": "topic",
    "topics": "topic",
    "how i write": "preference",
    "how to handle my mail": "rule",
    "facts the owner stated about themself": "fact",
    "how the owner writes": "preference",
}
_MODEL_SYSTEM = """You extract a private knowledge model for an email assistant.
Use only what the material states. Never invent names, numbers or dates.
Reply with JSON only, no markdown, in this shape:
{"items":[{"kind":"fact","text":"...","related":["optional other item text or name"],"source":"document title, profile or rules","as_of":"YYYY-MM-DD or empty"}]}
kind must be one of: fact, person, topic, experience, preference, rule, decision.
- fact: a concrete statement (price, date, role, address). These will be locked.
- person: a person or organisation and their relationship to the owner.
- topic: an ongoing subject, project or thread.
- experience: a pattern of how the owner has handled similar mail.
- preference: tone, sign-off, language, length.
- rule: an instruction the owner wants followed.
- decision: a call already made.
as_of is the date the material makes the statement true, if it says one. Leave it empty if the material has no date.
If the material says something used to be true but is no longer, still emit it with as_of and valid_until dates.
Keep each text under 220 characters. At most 48 items. Prefer fewer, clearer items over repeats."""

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
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    items = [_clean_item(item) for item in model.get("items") or [] if isinstance(item, dict)]
    items = [item for item in items if item]
    return {
        "docs": docs,
        "rules": rules,
        "brief": {
            "text": str(brief.get("text") or ""),
            "updated_at": str(brief.get("updated_at") or ""),
            "stale": bool(brief.get("stale")),
        },
        "model": {
            "items": items,
            "updated_at": str(model.get("updated_at") or ""),
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
            index["model"]["items"] = _sync_rules(index["model"]["items"], items)
            _save_index(index)
    return items


def save_brief(text: str) -> None:
    text = (text or "").strip()
    if len(text) > 12000:
        raise ConfigError("The understanding is too long. Keep it under 12,000 characters.")
    with LOCK:
        index = load_index()
        index["brief"] = {"text": text, "updated_at": _now(), "stale": False}
        if text and not index["model"]["items"]:
            index["model"] = {"items": items_from_brief(text), "updated_at": _now()}
        _save_index(index)


def rebuild_brief(profile: dict) -> str:
    """Re-read profile, rules and documents, then rewrite the model and the briefing."""
    return rebuild_model(profile)


def rebuild_model(profile: dict) -> str:
    """Rebuild the structured knowledge from current sources and settings."""
    previous = load_index()["model"]["items"]
    material = _source_material(profile)
    raw = complete(_MODEL_SYSTEM + "\n\nIf you cannot produce JSON, write the markdown briefing instead.", material, max_tokens=1400, temperature=0.2).strip()
    if not raw:
        raise ReplyError("The model returned nothing.")
    extracted = []
    parsed = parse_json(raw)
    if parsed:
        extracted = [_clean_item(item, origin="extract") for item in parsed.get("items") or [] if isinstance(item, dict)]
        extracted = [item for item in extracted if item]
    if not extracted and raw.lstrip().startswith("#"):
        extracted = items_from_brief(raw)
    if not extracted:
        briefing = complete(_BRIEF_SYSTEM, material, max_tokens=1100, temperature=0.2).strip()
        if not briefing:
            raise ReplyError("The model returned nothing.")
        extracted = items_from_brief(briefing)
    merged = _merge_items(extracted, previous)
    live = [item for item in merged if is_active(item)]
    text = brief_from_items(live, load_index()["rules"])
    with LOCK:
        index = load_index()
        index["model"] = {"items": merged, "updated_at": _now()}
        rules = [item["text"] for item in live if item["kind"] == "rule"]
        if rules:
            index["rules"] = rules[:MAX_RULES]
        index["brief"] = {"text": text, "updated_at": _now(), "stale": False}
        _save_index(index)
    return text


def list_items(kind: str = "", active_only: bool = False) -> list[dict]:
    items = load_index()["model"]["items"]
    if kind in KINDS:
        items = [item for item in items if item["kind"] == kind]
    if active_only:
        items = [item for item in items if is_active(item)]
    return items


def get_item(item_id: str) -> dict | None:
    for item in load_index()["model"]["items"]:
        if item["id"] == item_id:
            return item
    return None


def update_item(item_id: str, text: str) -> dict:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text or len(text) > 300:
        raise ConfigError("Keep each item between 1 and 300 characters.")
    with LOCK:
        index = load_index()
        found = None
        items = []
        for item in index["model"]["items"]:
            if item["id"] != item_id:
                items.append(item)
                continue
            if KINDS[item["kind"]]["locked"]:
                raise ConfigError("Facts are taken from your sources and cannot be edited. Rebuild knowledge to refresh them.")
            found = dict(item, text=text, origin="user", updated_at=_now())
            items.append(found)
        if found is None:
            raise ConfigError("That knowledge item is not here any more.")
        index["model"]["items"] = items
        index["model"]["updated_at"] = _now()
        if found["kind"] == "rule":
            index["rules"] = [item["text"] for item in items if item["kind"] == "rule"][:MAX_RULES]
        index["brief"]["stale"] = True
        _save_index(index)
    return found


def delete_item(item_id: str) -> bool:
    with LOCK:
        index = load_index()
        found = next((item for item in index["model"]["items"] if item["id"] == item_id), None)
        if found is None:
            return False
        if KINDS[found["kind"]]["locked"]:
            raise ConfigError("Facts cannot be deleted. Remove or change the source document, then rebuild knowledge.")
        index["model"]["items"] = [item for item in index["model"]["items"] if item["id"] != item_id]
        index["model"]["updated_at"] = _now()
        if found["kind"] == "rule":
            index["rules"] = [item["text"] for item in index["model"]["items"] if item["kind"] == "rule"]
        index["brief"]["stale"] = True
        _save_index(index)
    return True


def end_item(item_id: str) -> dict:
    """Mark an item as no longer true without rewriting it. Facts can end; they still cannot be edited."""
    with LOCK:
        index = load_index()
        found = None
        items = []
        for item in index["model"]["items"]:
            if item["id"] != item_id:
                items.append(item)
                continue
            found = dict(item, status="ended", valid_until=item.get("valid_until") or _now(), updated_at=_now())
            items.append(found)
        if found is None:
            raise ConfigError("That knowledge item is not here any more.")
        index["model"]["items"] = items
        index["model"]["updated_at"] = _now()
        if found["kind"] == "rule":
            index["rules"] = [item["text"] for item in items if item["kind"] == "rule" and is_active(item)]
        index["brief"]["stale"] = True
        _save_index(index)
    return found


def add_item(kind: str, text: str) -> dict:
    if kind not in KINDS or KINDS[kind]["locked"]:
        raise ConfigError("That kind of knowledge cannot be added by hand.")
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text or len(text) > 300:
        raise ConfigError("Keep each item between 1 and 300 characters.")
    item = _clean_item({"kind": kind, "text": text, "origin": "user"}, origin="user")
    with LOCK:
        index = load_index()
        if len(index["model"]["items"]) >= 80:
            raise ConfigError("Too many knowledge items. Delete some first, or rebuild to compress them.")
        index["model"]["items"].append(item)
        index["model"]["updated_at"] = _now()
        if kind == "rule":
            rules = index["rules"] + [text]
            if len(rules) > MAX_RULES:
                raise ConfigError(f"Too many rules. Keep it to {MAX_RULES} or fewer.")
            index["rules"] = rules
        index["brief"]["stale"] = True
        _save_index(index)
    return item


def items_from_brief(text: str) -> list[dict]:
    """Turn the older markdown briefing into cards so an existing base still has a map."""
    items = []
    kind = "fact"
    for line in (text or "").splitlines():
        heading = re.match(r"^#{1,3}\s+(.+)$", line.strip())
        if heading:
            kind = _HEADING_KIND.get(heading.group(1).strip().lower(), kind)
            continue
        bullet = re.match(r"^[-•*]\s+(.+)$", line.strip())
        if not bullet:
            continue
        body = bullet.group(1).strip()
        if not body or body.lower().startswith("nothing recorded"):
            continue
        items.append(_clean_item({"kind": kind, "text": body, "origin": "extract"}, origin="extract"))
    return [item for item in items if item]


def is_active(item: dict) -> bool:
    status = str(item.get("status") or "active")
    if status in ("ended", "superseded"):
        return False
    until = str(item.get("valid_until") or "").strip()
    if until and until <= _now():
        return False
    return True


def brief_from_items(items: list[dict], rules: list[str] | None = None) -> str:
    groups = {kind: [] for kind in _KIND_ORDER}
    for item in items:
        if not is_active(item):
            continue
        groups.get(item["kind"], groups["fact"]).append(item["text"])
    if rules:
        for rule in rules:
            if rule not in groups["rule"]:
                groups["rule"].insert(0, rule)

    def block(title: str, lines: list[str]) -> str:
        bullets = "\n".join(f"- {line}" for line in lines[:12]) if lines else "- Nothing recorded yet."
        return f"## {title}\n{bullets}"

    return "\n\n".join(
        [
            block("Who I am", groups["fact"][:4]),
            block("Key facts", groups["fact"][4:]),
            block("People and organisations", groups["person"]),
            block("Ongoing topics and commitments", groups["topic"] + groups["decision"]),
            block("How I write", groups["preference"] + groups["experience"]),
            block("How to handle my mail", groups["rule"]),
        ]
    )


def graph_view(items: list[dict] | None = None) -> dict:
    """Topology for the D3 map: you, kind hubs, and current items. Layout is done in the browser."""
    items = items if items is not None else load_index()["model"]["items"]
    items = [item for item in items if is_active(item)]
    present = [kind for kind in _KIND_ORDER if any(item["kind"] == kind for item in items)]
    nodes = [
        {
            "id": "you",
            "role": "you",
            "kind": "you",
            "label": "You",
            "title": "Your profile and this knowledge base",
            "href": "#cards",
        }
    ]
    edges = []
    for kind in present:
        hub = f"kind-{kind}"
        nodes.append(
            {
                "id": hub,
                "role": "hub",
                "kind": kind,
                "label": KINDS[kind]["label"],
                "title": KINDS[kind]["hint"],
                "href": f"/knowledge?kind={kind}#cards",
            }
        )
        edges.append({"from": "you", "to": hub, "related": False})
        for item in [row for row in items if row["kind"] == kind][:8]:
            nodes.append(
                {
                    "id": item["id"],
                    "role": "leaf",
                    "kind": kind,
                    "label": _short_label(item["text"]),
                    "title": item["text"],
                    "href": f"/knowledge?kind={kind}#item-{item['id']}",
                }
            )
            edges.append({"from": hub, "to": item["id"], "related": False})
            for other in item.get("related") or []:
                match = next((node for node in nodes if node["title"] == other or node["label"] == other), None)
                if match and match["id"] != item["id"]:
                    edges.append({"from": item["id"], "to": match["id"], "related": True})
    return {"nodes": nodes, "edges": edges}


def memory_tiles(items: list[dict] | None = None, preview: int = 4) -> list[dict]:
    """Compact Memory view: one tile per kind, with the live bullets on top."""
    items = items if items is not None else load_index()["model"]["items"]
    live = [item for item in items if is_active(item)]
    tiles = []
    for kind in _KIND_ORDER:
        cards = [item for item in live if item["kind"] == kind]
        tiles.append(
            {
                "kind": kind,
                "label": KINDS[kind]["label"],
                "locked": KINDS[kind]["locked"],
                "hint": KINDS[kind]["hint"],
                "count": len(cards),
                "preview": cards[:preview],
                "more": max(0, len(cards) - preview),
            }
        )
    return tiles


def timeline(items: list[dict] | None = None) -> list[dict]:
    """Newest-first days. Each event is when something became true, was replaced, or ended."""
    items = items if items is not None else load_index()["model"]["items"]
    events = []
    by_id = {item["id"]: item for item in items}
    for item in items:
        events.append(
            {
                "when": item.get("at") or item.get("valid_from") or "",
                "action": "added",
                "label": "Became current",
                "item": item,
            }
        )
        if item.get("replaces") and item["replaces"] in by_id:
            events.append(
                {
                    "when": item.get("valid_from") or item.get("at") or "",
                    "action": "superseded",
                    "label": "Replaced an earlier version",
                    "item": item,
                    "previous": by_id[item["replaces"]],
                }
            )
        if item.get("valid_until"):
            events.append(
                {
                    "when": item["valid_until"],
                    "action": item.get("status") or "ended",
                    "label": "No longer current",
                    "item": item,
                }
            )
    events = [event for event in events if event["when"]]
    events.sort(key=lambda event: event["when"], reverse=True)
    days = []
    for event in events:
        day = event["when"][:10]
        if not days or days[-1]["day"] != day:
            days.append({"day": day, "events": []})
        days[-1]["events"].append(event)
    return days


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
    live = [item for item in index["model"]["items"] if is_active(item)]
    past = [item for item in index["model"]["items"] if not is_active(item)]
    by_kind = {kind: 0 for kind in _KIND_ORDER}
    for item in live:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {
        "documents": len(index["docs"]),
        "by_source": counts,
        "rules": len(index["rules"]),
        "item_count": len(live),
        "past_count": len(past),
        "by_kind": by_kind,
        "brief_ready": bool(index["brief"]["text"] or live),
        "brief_stale": index["brief"]["stale"],
        "characters": sum(int(item.get("chars", 0) or 0) for item in index["docs"]),
        "model_updated": index["model"]["updated_at"],
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
    save_doc("kb_index", {key: index[key] for key in ("docs", "rules", "brief", "model", "stamp")})


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


def _source_material(profile: dict) -> str:
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
    return "\n\n".join(parts)


def _clean_item(raw: dict, origin: str = "") -> dict | None:
    kind = str(raw.get("kind") or "fact").strip().lower()
    if kind not in KINDS:
        kind = "fact"
    text = re.sub(r"\s+", " ", str(raw.get("text") or "").strip())
    if not text or text.lower().startswith("nothing recorded"):
        return None
    related = []
    for item in raw.get("related") or []:
        label = re.sub(r"\s+", " ", str(item).strip())
        if label and label not in related:
            related.append(label[:80])
    item_id = re.sub(r"[^a-zA-Z0-9]", "", str(raw.get("id") or ""))[:16]
    now = _now()
    at = _as_of(raw.get("at") or raw.get("as_of") or raw.get("valid_from")) or now
    valid_from = _as_of(raw.get("valid_from") or raw.get("as_of")) or at
    valid_until = _as_of(raw.get("valid_until"))
    status = str(raw.get("status") or "active").strip().lower()
    if status not in ("active", "superseded", "ended"):
        status = "ended" if valid_until else "active"
    return {
        "id": item_id or secrets.token_hex(5),
        "kind": kind,
        "text": text[:300],
        "related": related[:6],
        "source": str(raw.get("source") or "").strip()[:160],
        "origin": origin or str(raw.get("origin") or "extract"),
        "locked": bool(KINDS[kind]["locked"]),
        "at": at,
        "updated_at": _as_of(raw.get("updated_at")) or at,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "status": status,
        "replaces": str(raw.get("replaces") or "")[:16],
        "key": _item_key({"kind": kind, "text": text}),
    }


def _sync_rules(items: list[dict], rules: list[str]) -> list[dict]:
    kept = [item for item in items if item["kind"] != "rule" or item.get("origin") == "extract"]
    have = {item["text"] for item in kept if item["kind"] == "rule"}
    for rule in rules:
        if rule in have:
            continue
        kept.append(_clean_item({"kind": "rule", "text": rule, "origin": "user"}, origin="user"))
    return [item for item in kept if item]


def _merge_items(extracted: list[dict], previous: list[dict]) -> list[dict]:
    """Keep history. Same-topic facts with new wording supersede the old ones instead of deleting them."""
    now = _now()
    live = [item for item in previous if is_active(item)]
    history = [item for item in previous if not is_active(item)]
    taken: set[str] = set()
    out: list[dict] = []
    for new in extracted:
        if not new:
            continue
        old = next((item for item in live if item["id"] not in taken and _item_key(item) == _item_key(new)), None)
        if old is None:
            out.append(new)
            continue
        taken.add(old["id"])
        if old["text"].strip().lower() == new["text"].strip().lower():
            out.append(old)
            continue
        out.append(dict(old, status="superseded", valid_until=now, updated_at=now))
        new["replaces"] = old["id"]
        new["at"] = now
        new["valid_from"] = now
        new["updated_at"] = now
        out.append(new)
    for item in live:
        if item["id"] in taken:
            continue
        if item.get("origin") == "user" or item["kind"] != "fact":
            out.append(item)
            continue
        out.append(dict(item, status="ended", valid_until=now, updated_at=now))
    seen = {(item["id"]) for item in out}
    for item in history:
        if item["id"] not in seen:
            out.append(item)
            seen.add(item["id"])
    return out[:120]


def _item_key(item: dict) -> str:
    words = [word for word in _tokens(item.get("text") or "") if not word.isdigit()]
    return f"{item.get('kind', 'fact')}:{' '.join(words[:6])}"


def _as_of(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?", text)
    if not match:
        return ""
    return f"{match.group(1)} {match.group(2) or '00:00'}"


def _short_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= 22 else text[:21].rstrip() + "…"

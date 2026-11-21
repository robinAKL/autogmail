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
from datetime import datetime

from gmail_auto.envfile import has_openai_key
from gmail_auto.errors import ConfigError, ReplyError, named_error
from gmail_auto.filters import is_sensitive
from gmail_auto.llm import complete, parse_json
from gmail_auto.profile_store import profile_as_prompt
from gmail_auto.storage import LOCK, delete_doc, load_doc, save_doc, workspace_operation
from gmail_auto.tenant import saas_enabled

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
    "storyline": {
        "label": "Storylines",
        "locked": False,
        "hint": "A thread you have replied in more than once. Later replies can follow the arc.",
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
            # A manual review/end/preference decision must take effect immediately,
            # even before the next model-assisted reorganisation.
            "text": brief_from_items(items, rules) if brief.get('stale') and items else str(brief.get("text") or ""),
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
    if existing is not None:
        _forget_file(existing, keep=(meta or {}).get("file_id", ""))
    return entry


ROLES = {
    "reference": "Reference",
    "price_list": "Price list",
    "brochure": "Company introduction",
    "product_info": "Product or service info",
    "invoice_template": "Invoice template",
    "contract_template": "Contract template",
}
# Files the owner sends to people as they are. They are facts for replies too (unlike templates).
SEND_ROLES = ("brochure", "product_info", "price_list")
# A template is an example of a document, not facts: an old invoice names another client and their amounts.
TEMPLATE_ROLES = {"invoice_template", "contract_template"}


def role_of(item: dict) -> str:
    role = (item.get("meta") or {}).get("role") or "reference"
    return role if role in ROLES else "reference"


def is_template(item: dict) -> bool:
    return role_of(item) in TEMPLATE_ROLES


def templates(role: str) -> list[dict]:
    return [item for item in list_documents() if role_of(item) == role]


def update_meta(doc_id: str, changes: dict) -> dict:
    """Change a document's type or template details. Replies are rebuilt without templates."""
    with LOCK:
        index = load_index()
        entry = next((item for item in index["docs"] if item["id"] == doc_id), None)
        if entry is None:
            raise ConfigError("That document is no longer in the knowledge base.")
        entry["meta"] = {**(entry.get("meta") or {}), **changes}
        index["brief"]["stale"] = True
        _save_index(index)
        return entry


_INVOICE_HINT = re.compile(r"(?i)\b(tax invoice|invoice (?:no|number|#|date)|bill to|amount due|invoice total|gst no)\b")
_CONTRACT_HINT = re.compile(
    r"(?i)\b(this agreement|agreement between|the parties|hereby|terms and conditions|in witness|signed by|"
    r"governing law|termination|indemnif\w+|confidentiality|commencement date)\b|合同|协议|甲方|乙方"
)
_BROCHURE_HINT = re.compile(r"(?i)\b(company profile|about us|capability statement|who we are|our story|company introduction)\b|公司介绍|公司简介|关于我们")
_PRODUCT_HINT = re.compile(r"(?i)\b(product (?:overview|brochure|sheet|deck)|course outline|service overview|spec(?:ification)? sheet|features and benefits|catalogue)\b|产品介绍|课程介绍|服务介绍")
_PRICE_HINT = re.compile(r"(?i)\b(price list|pricing|our rates|fee schedule|rate card|per hour|per session|per person)\b|价目|报价单|收费标准")


def classify(title: str, text: str, complete=None) -> str:
    """Decide whether an upload is an invoice template, a contract template, a price list or plain reference."""
    from gmail_auto.llm import model_ready, parse_json

    head = f"{title}\n{(text or '')[:5000]}"
    if complete is None:
        complete = _complete_for_classify() if model_ready() else None
    if complete is not None:
        try:
            raw = complete(
                "You sort a document the owner uploaded to their business knowledge base. Answer with JSON only: "
                '{"role": "invoice_template" | "contract_template" | "price_list" | "brochure" | "product_info" | "reference"}. '
                "invoice_template: an invoice or tax invoice the owner sent (or a blank one), useful as a layout. "
                "contract_template: an agreement, contract or terms the owner uses with clients, filled or blank. "
                "price_list: mainly the owner's prices, rates or packages. "
                "brochure: an introduction to the owner's company (about us, capability statement, company profile). "
                "product_info: a description of one of the owner's products, services or courses (deck, flyer, "
                "spec sheet, course outline) meant for clients. reference: anything else, including internal papers.",
                head,
                max_tokens=60,
                temperature=0,
            )
            role = str((parse_json(raw) or {}).get("role") or "").strip()
            if role in ROLES:
                return role
        except ReplyError:
            pass
    if len(_INVOICE_HINT.findall(head)) >= 2:
        return "invoice_template"
    if len(set(match.lower() for match in _CONTRACT_HINT.findall(head))) >= 3:
        return "contract_template"
    if _PRICE_HINT.search(head):
        return "price_list"
    if _BROCHURE_HINT.search(head):
        return "brochure"
    if _PRODUCT_HINT.search(head):
        return "product_info"
    return "reference"


def _complete_for_classify():
    from gmail_auto.llm import complete

    return complete


def attachable_documents() -> list[dict]:
    """Knowledge documents whose original file was kept, so they can be attached to a reply."""
    # Templates are left out: an old invoice or contract names another client.
    kept = [item for item in list_documents() if (item.get("meta") or {}).get("file_id") and not is_template(item)]
    kept.sort(key=lambda item: 0 if role_of(item) in SEND_ROLES else 1)
    return kept


def text_only_uploads() -> list[dict]:
    """Uploaded files whose original was not kept (too big, or uploaded before file storage): they can be
    read but not attached until they are uploaded again."""
    return [
        item for item in list_documents()
        if item.get("source") == "upload" and not (item.get("meta") or {}).get("file_id") and not is_template(item)
    ]


def _forget_file(entry: dict, keep: str = "") -> None:
    file_id = (entry.get("meta") or {}).get("file_id") or ""
    if file_id and file_id != keep:
        from gmail_auto import files

        files.remove(file_id)


def delete_document(doc_id: str) -> bool:
    with LOCK:
        index = load_index()
        kept = [item for item in index["docs"] if item["id"] != doc_id]
        if len(kept) == len(index["docs"]):
            return False
        gone = next(item for item in index["docs"] if item["id"] == doc_id)
        index["docs"] = kept
        index["brief"]["stale"] = True
        _save_index(index)
        delete_doc(_body_name(doc_id))
    _forget_file(gone)
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
        rules = [item["text"] for item in live if item["kind"] == "rule" and item["origin"] != "analysis"]
        if rules:
            index["rules"] = rules[:MAX_RULES]
        index["brief"] = {"text": text, "updated_at": _now(), "stale": False}
        _save_index(index)
    return text


def merge_analysis(extracted: list[dict]) -> None:
    """Merge event-backed cards while preserving user edits and multiple periods of a fact."""
    if not extracted:
        return
    with workspace_operation("kb_index_analysis"), LOCK:
        index = load_index()
        items = index["model"]["items"]
        by_key = {_analysis_key(item): pos for pos, item in enumerate(items)}
        rule_texts = []
        for raw in extracted:
            if str(raw.get("kind") or "").strip().lower() in ("rule", "preference"):
                rule_texts.append(raw.get("text") or "")
                continue
            item = _clean_item(raw, origin="analysis")
            if not item:
                continue
            key = _analysis_key(item)
            if key in by_key:
                old = items[by_key[key]]
                if old["origin"] != "analysis" or old["status"] != "active":
                    continue
                if item["kind"] == "storyline":
                    items[by_key[key]] = dict(
                        old,
                        text=item["text"],
                        source=item["source"],
                        evidence=item["evidence"],
                        steps=item.get("steps") or [],
                        observed_at=item.get("observed_at") or old.get("observed_at") or "",
                        updated_at=_now(),
                    )
                    continue
                sources = {source["source_id"]: source for source in old["evidence"] + item["evidence"]}
                latest = sorted(sources.values(), key=lambda source: source["observed_at"], reverse=True)[:5]
                if latest:
                    primary = latest[0]
                    items[by_key[key]] = dict(
                        old, evidence=latest, observed_at=primary["observed_at"], source=primary["source"],
                    )
            else:
                by_key[key] = len(items)
                items.append(item)
        if rule_texts:
            index["rules"] = _extend_rules(index["rules"], rule_texts)
            items = _sync_rules(items, index["rules"])
        now = _now()
        index["model"]["items"] = items
        index["model"]["updated_at"] = now
        index["brief"] = {"text": brief_from_items(items, index["rules"]), "updated_at": now, "stale": False}
        _save_index(index)


def _story_steps(raw: dict) -> list[dict]:
    steps = []
    for source in (raw.get("steps") if isinstance(raw.get("steps"), list) else [])[:40]:
        if not isinstance(source, dict):
            continue
        summary = re.sub(r"\s+", " ", str(source.get("summary") or "").strip())
        if not summary:
            continue
        steps.append({
            "at": _as_of(source.get("at")),
            "who": str(source.get("who") or "")[:80],
            "outbound": bool(source.get("outbound")),
            "summary": summary[:160],
        })
    return steps


def _analysis_key(item: dict) -> tuple[str, ...]:
    if item.get("kind") == "storyline" and item.get("evidence"):
        return ("storyline", str(item["evidence"][0].get("source_id") or ""))
    return (
        item["kind"], item["text"].strip().casefold(), item.get("temporal", ""),
        item.get("valid_from", "")[:10] if item.get("temporal") == "dated" else "",
        item.get("valid_until", "")[:10] if item.get("temporal") == "dated" else "",
    )


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
            index["rules"] = [item["text"] for item in items if item["kind"] == "rule" and item["origin"] != "analysis"][:MAX_RULES]
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
            index["rules"] = [item["text"] for item in index["model"]["items"] if item["kind"] == "rule" and item["origin"] != "analysis"]
        index["brief"]["stale"] = True
        _save_index(index)
    return True


def restore_review_items(previous: list[dict]) -> None:
    """Undo review status changes without rewriting extracted fact wording."""
    with LOCK:
        index = load_index()
        for old in previous:
            item = next((i for i in index['model']['items'] if i['id']==old['id']),None)
            if not item or item['text'] != old['text']:
                raise ConfigError('The knowledge base was reorganized, so this cannot be undone safely. Review it by hand.')
            for key in ('status','valid_until','confirmed_at'):
                item[key] = old.get(key,'')
        index['brief']['stale'] = True
        _save_index(index)


def confirm_item(item_id: str, valid_until: str = '') -> dict:
    from datetime import date
    if valid_until:
        try:
            date.fromisoformat(valid_until)
        except ValueError as exc:
            raise ConfigError('Enter the valid-until date as YYYY-MM-DD.') from exc
        if valid_until <= date.today().isoformat():
            raise ConfigError('The valid-until date must be in the future.')
    with LOCK:
        index = load_index()
        item = next((i for i in index['model']['items'] if i['id']==item_id),None)
        if not item:
            raise ConfigError('This knowledge item has changed. Refresh the page.')
        item['confirmed_at'] = _now()
        item['valid_until'] = valid_until
        item['status'] = 'active'
        item['updated_at'] = _now()
        index['brief']['stale'] = True
        _save_index(index)
        return item


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
            index["rules"] = [item["text"] for item in items if item["kind"] == "rule" and item["origin"] != "analysis" and is_active(item)]
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


def is_shown(item: dict) -> bool:
    """Rules stay in the editable list. A mail date alone does not make a fact."""
    if item.get("kind") == "rule":
        return False
    return not (
        item.get("kind") == "fact"
        and item.get("origin") == "analysis"
        and item.get("temporal") == "unknown"
    )


def is_active(item: dict) -> bool:
    status = str(item.get("status") or "active")
    if status in ("ended", "superseded"):
        return False
    until = str(item.get("valid_until") or "").strip()
    if item.get("temporal") == "dated":
        return not until or until[:10] >= _now()[:10]
    if until and until <= _now():
        return False
    return True


def brief_from_items(items: list[dict], rules: list[str] | None = None) -> str:
    groups = {kind: [] for kind in _KIND_ORDER}
    for item in items:
        if not is_active(item):
            continue
        if item.get("origin") == "analysis":
            if item.get("temporal") != "stable":
                continue
            if item["kind"] == "rule":
                groups["experience"].append(f"Observed reply practice: {item['text']}")
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
            block("Storylines", groups["storyline"][:8]),
        ]
    )


def graph_view(items: list[dict] | None = None) -> dict:
    """Topology for the D3 map: you, kind hubs, and current items. Layout is done in the browser."""
    from gmail_auto.story import display_title

    items = items if items is not None else load_index()["model"]["items"]
    items = [item for item in items if is_shown(item) and is_active(item)]
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
                "href": "#storylines" if kind == "storyline" else f"/knowledge?kind={kind}#cards",
            }
        )
        edges.append({"from": "you", "to": hub, "related": False})
        for item in [row for row in items if row["kind"] == kind][:8]:
            label = item["text"]
            if kind == "storyline":
                label = _storyline_card(item, display_title)["title"]
            nodes.append(
                {
                    "id": item["id"],
                    "role": "leaf",
                    "kind": kind,
                    "label": _short_label(label),
                    "title": label,
                    "href": ("#item-" + item["id"]) if kind == "storyline" else f"/knowledge?kind={kind}#item-{item['id']}",
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
    live = [item for item in items if is_shown(item) and is_active(item)]
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


_STORY_TEXT = re.compile(
    r"^With (?P<who>.+?) about (?P<title>.+?)\. (?P<count>\d+) letters, (?P<sent>\d+) written by you\.(?: Latest: (?P<latest>.+))?$"
)
_STORY_STATUS = {
    "waiting_on_me": "Waiting on you",
    "waiting_on_them": "Waiting on them",
    "in_progress": "In progress",
}


def storyline_cards(items: list[dict] | None = None) -> list[dict]:
    """Compact storylines. The list shows title, person and status. The timeline stays closed."""
    from gmail_auto.story import display_title

    items = items if items is not None else load_index()["model"]["items"]
    cards = []
    for item in items:
        if item.get("kind") != "storyline" or not is_shown(item) or not is_active(item):
            continue
        cards.append(_storyline_card(item, display_title))
    cards.sort(key=lambda card: (card["date"] or "", card["title"].casefold()), reverse=True)
    return cards


def _storyline_card(item: dict, display_title) -> dict:
    who, title, count, sent, latest = _storyline_parts(item)
    steps = []
    for raw in item.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        summary = re.sub(r"\s+", " ", str(raw.get("summary") or "")).strip()
        if not summary:
            continue
        when = _as_of(raw.get("at")) or _date_in_text(summary)
        summary = _story_sentence(summary)
        steps.append({
            "who": str(raw.get("who") or ("You" if raw.get("outbound") else who))[:80],
            "date": when,
            "outbound": bool(raw.get("outbound")),
            "summary": summary[:160],
        })
    if not steps and latest:
        sentence = _story_sentence(latest)
        steps.append({
            "who": "You", "date": _date_in_text(latest), "outbound": True, "summary": sentence[:160],
        })
    steps.sort(key=lambda step: step["date"])
    newest = steps[-1] if steps else {}
    if newest.get("outbound"):
        status = "waiting_on_them"
    elif newest:
        status = "waiting_on_me"
    else:
        status = "in_progress"
    for index, step in enumerate(steps):
        step["current"] = index == len(steps) - 1
        step["day"] = _story_day(step["date"])
    return {
        "id": item.get("id") or "",
        "title": display_title(title or item.get("source") or ""),
        "who": _story_person(who),
        "status": status,
        "label": _STORY_STATUS[status],
        "size": count,
        "sent": sent,
        "date": newest.get("date") or "",
        "day": _story_day(newest.get("date") or ""),
        "steps": steps,
    }


def _storyline_parts(item: dict) -> tuple[str, str, int, int, str]:
    match = _STORY_TEXT.match(re.sub(r"\s+", " ", str(item.get("text") or "").strip()))
    if not match:
        return "", item.get("source") or item.get("text") or "", 0, 0, ""
    return (
        match.group("who"),
        match.group("title"),
        int(match.group("count")),
        int(match.group("sent")),
        (match.group("latest") or "").strip(),
    )


_STORY_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _story_sentence(text: str) -> str:
    line = " ".join(str(text or "").split())
    line = re.sub(r"(?i)^subject:\s*", "", line)
    line = re.split(r"\s+On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,", line, maxsplit=1)[0]
    line = re.split(r"\s+On\s+.{0,80}?wrote:", line, maxsplit=1)[0]
    return line.strip()


def _date_in_text(text: str) -> str:
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    match = re.search(
        r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b",
        str(text or ""),
        re.I,
    )
    if not match:
        return ""
    day = int(match.group(1))
    name = match.group(2).lower()
    month = months["sept" if name.startswith("sept") else name[:3]]
    if not 1 <= day <= 31:
        return ""
    return f"{int(match.group(3)):04d}-{month:02d}-{day:02d} 00:00"


def _story_day(value: str) -> str:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return ""
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return ""
    return f"{int(match.group(3))} {_STORY_MONTHS[month]}"


def _story_person(value: str) -> str:
    text = " ".join(str(value or "").replace(";", ",").split())
    if not text:
        return "The correspondent"
    first = text.split(",")[0].strip()
    name = first.split("<", 1)[0].strip().strip('"')
    if name and "@" not in name:
        return name
    address = first
    if "<" in first and ">" in first:
        address = first.split("<", 1)[1].split(">", 1)[0]
    return address.split("@", 1)[0] or "The correspondent"


def timeline(items: list[dict] | None = None) -> list[dict]:
    """Newest-first days. Each event is when something became true, was replaced, or ended."""
    items = items if items is not None else load_index()["model"]["items"]
    items = [item for item in items if is_shown(item)]
    events = []
    by_id = {item["id"]: item for item in items}
    for item in items:
        events.append(
            {
                "when": item.get("at") or item.get("valid_from") or "",
                "action": "added",
                "label": "Observed in a source" if item.get("origin") == "analysis" else "Became current",
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
    if any(item["origin"] == "analysis" for item in index["model"]["items"]):
        save_brief(brief_from_items(index["model"]["items"], index["rules"]))
        return
    try:
        rebuild_brief(profile)
    except ConfigError:
        return


# ---- learning from past email ------------------------------------------------


def learn_from_mailbox(client, limit: int = 30) -> dict:
    """Read recent sent and received mail and keep a summary of what it shows."""
    account = client.get_user_email()
    provider = getattr(client, "provider", "gmail")
    # Outlook Graph 502s or times out when HTML bodies are pulled in bulk.
    take = max(1, min(int(limit), 12 if provider == "outlook" else 25))
    want_full = provider != "outlook"
    sent, received, problems = [], [], []

    def collect(fetch):
        try:
            return list(fetch()), ""
        except ConfigError as exc:
            return [], str(exc)

    from concurrent.futures import ThreadPoolExecutor

    # Received mail still in the inbox. Archived mail and junk categories stay out.
    receive = getattr(client, "list_received_history", None)
    if not callable(receive):
        receive = client.list_recent
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_sent = pool.submit(collect, lambda: client.list_sent(take, full=want_full))
        future_received = pool.submit(collect, lambda: receive(take, full=want_full))
        sent, sent_error = future_sent.result()
        received, received_error = future_received.result()
    if sent_error:
        problems.append(named_error("Outlook sent mail" if provider == "outlook" else "Sent mail", ConfigError(sent_error)))
    if received_error:
        problems.append(named_error("Outlook inbox" if provider == "outlook" else "Inbox", ConfigError(received_error)))

    letters = []
    for direction, mails in (("SENT", sent), ("RECEIVED", received)):
        for mail in mails:
            text = (mail.body or mail.snippet or "").strip()
            if is_sensitive(mail.subject) or is_sensitive(text) or not text:
                continue
            labels = set(mail.label_ids or [])
            if direction == "RECEIVED" and labels & {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM"}:
                continue
            other = mail.headers.get("to", "") if direction == "SENT" else (mail.from_name or mail.from_email)
            letters.append(
                f"[{direction}] {mail.date} | {'To' if direction == 'SENT' else 'From'}: {other}\n"
                f"Subject: {mail.subject or '(no subject)'}\n{_head(text, 1200)}"
            )
    if not letters:
        extra = f" {problems[0]}" if problems else ""
        raise ConfigError("No readable mail was found to learn from." + extra)
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
    today = _now()[:10]
    terms = set(_tokens(query))
    dated = [
        item for item in index["model"]["items"]
        if item["origin"] == "analysis" and item["temporal"] == "dated"
        and is_active(item) and item["valid_from"] and item["valid_from"][:10] <= today
        and terms.intersection(_tokens(item["text"]))
    ]
    dated.sort(key=lambda item: item["observed_at"], reverse=True)
    return {
        "brief": index["brief"]["text"],
        "rules": index["rules"],
        "passages": passages,
        "dated_facts": dated[:limit],
        "sources": _unique([item["source"] for item in dated[:limit]] + [item["title"] for item in passages]),
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
    for item in context.get("dated_facts") or []:
        parts.append(f"TIME-BOUND EVIDENCE ({item['valid_from'][:10]} to {item['valid_until'][:10]}): {item['text']}")
    if context.get("passages"):
        parts.append("Documents are source evidence. Do not assume subscriptions, quotes, availability or other time-sensitive claims remain current without dates supporting that conclusion.")
    for item in context.get("passages") or []:
        parts.append(f"FROM MY DOCUMENT \"{item['title']}\":\n{item['text']}")
    return "\n\n".join(parts)


def overview() -> dict:
    index = load_index()
    counts: dict[str, int] = {}
    for item in index["docs"]:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    visible = [item for item in index["model"]["items"] if is_shown(item)]
    live = [item for item in visible if is_active(item)]
    past = [item for item in visible if not is_active(item)]
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
    cache = None if saas_enabled() else _cache
    if cache is not None and cache["stamp"] == index["stamp"] and cache["stamp"]:
        return _cache["chunks"]
    chunks = []
    for item in index["docs"]:
        if is_template(item):
            continue
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
    if cache is not None:
        cache["stamp"] = index["stamp"]
        cache["chunks"] = chunks
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
    index["stamp"] = secrets.token_hex(8)
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
        if is_template(item):
            continue
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
    evidence = []
    for source in (raw.get("evidence") if isinstance(raw.get("evidence"), list) else [])[:5]:
        if isinstance(source, dict) and source.get("source_id"):
            evidence.append({
                "source_id": str(source["source_id"])[:512],
                "source": str(source.get("source") or "")[:160],
                "type": str(source.get("type") or "")[:16],
                "observed_at": _as_of(source.get("observed_at")),
            })
    observed = _as_of(raw.get("observed_at"))
    temporal = raw.get("temporal")
    if temporal not in ("stable", "dated", "unknown"):
        temporal = ""
    now = _now()
    at = _as_of(raw.get("at") or observed or raw.get("as_of") or raw.get("valid_from")) or now
    valid_from = _as_of(raw.get("valid_from") or raw.get("as_of")) or ("" if temporal else at)
    valid_until = _as_of(raw.get("valid_until"))
    status = str(raw.get("status") or "active").strip().lower()
    if status not in ("active", "superseded", "ended"):
        status = "ended" if valid_until else "active"
    built = {
        "id": item_id or secrets.token_hex(5),
        "kind": kind,
        "text": text[:300],
        "related": related[:6],
        "source": str(raw.get("source") or "").strip()[:160],
        "evidence": evidence,
        "observed_at": observed,
        "temporal": temporal,
        "recipient": str(raw.get("recipient") or "").strip()[:160],
        "tone": str(raw.get("tone") or "").strip()[:80],
        "origin": origin or str(raw.get("origin") or "extract"),
        "locked": bool(KINDS[kind]["locked"]),
        "at": at,
        "updated_at": _as_of(raw.get("updated_at")) or at,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "confirmed_at": str(raw.get('confirmed_at') or ''),
        "status": status,
        "replaces": str(raw.get("replaces") or "")[:16],
        "key": _item_key({"kind": kind, "text": text}),
    }
    if kind == "storyline":
        built["steps"] = _story_steps(raw)
    return built


def _extend_rules(rules: list[str], texts: list[str]) -> list[str]:
    """Add reply rules the user can still edit. Existing lines stay first."""
    seen = {rule.strip().casefold() for rule in rules}
    out = list(rules)
    for raw in texts:
        text = re.sub(r"\s+", " ", str(raw or "").strip())[:300]
        if not text or text.casefold() in seen or len(out) >= MAX_RULES:
            continue
        out.append(text)
        seen.add(text.casefold())
    return out


def _sync_rules(items: list[dict], rules: list[str]) -> list[dict]:
    kept = [item for item in items if item["kind"] != "rule" or item.get("origin") in ("extract", "analysis")]
    have = {item["text"] for item in kept if item["kind"] == "rule"}
    for rule in rules:
        if rule in have:
            continue
        kept.append(_clean_item({"kind": "rule", "text": rule, "origin": "user"}, origin="user"))
    return [item for item in kept if item]


def _merge_items(extracted: list[dict], previous: list[dict]) -> list[dict]:
    """Keep history. Same-topic facts with new wording supersede the old ones instead of deleting them."""
    now = _now()
    live = [item for item in previous if is_active(item) and item["origin"] != "analysis"]
    history = [item for item in previous if not is_active(item) or item["origin"] == "analysis"]
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

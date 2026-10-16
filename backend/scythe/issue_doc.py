"""Scythe's storage: plain files in git, mirrored to GitHub issues.

Files are the record (every save is a commit; a stale save is rejected like a non-fast-forward
push, which the app shows as a divergence):

    cards/<n>.md     JSON front matter (title, topic, file, block, approved, cite, url, source,
                     links, spans one-per-line, verbatim text), then the card as Scythe Markdown
    sources/<n>.md   JSON front matter (title, url, authors, ...), then the verbatim text
    scythe.json      shared settings: labels, custom topics

<n> is the number of the card's (or source's) GitHub issue, which the app opens first. Issues are
the readable, searchable, discussable view: the spaCy Action rewrites a card's issue from its file
on every push (title, labels, body = rendered card + `<!-- scythe:v1 {...} -->` metadata with
spaCy token offsets and entities). Pure (stdlib only): shared by the Action and the local index,
and mirrored in frontend/src/lib/doc.ts.
"""

import hashlib
import json
import re
from types import SimpleNamespace

from .markdown import body_markdown, card_markdown, cite_markdown

META_RE = re.compile(r"<!-- scythe:v1\n(.*?)\n-->", re.S)
TEXT_RE = re.compile(r"<!-- scythe:text -->\n(.*?)\n<!-- /scythe:text -->", re.S)
PART_RE = re.compile(r"<!-- scythe:part (\d+) -->\n(.*?)\n<!-- /scythe:part -->", re.S)
KIND_LABELS = {"card": "scythe:card", "source": "scythe:source", "topic": "scythe:topic", "config": "scythe:config"}
BODY_LIMIT = 65536
PART_SIZE = 60000
DEFAULT_LABELS = [
    {"name": "underline", "key": "sc", "style": "underline", "hotkey": "1"},
    {"name": "highlight", "key": "sc", "style": "highlight", "hotkey": "2"},
    {"name": "emphasis", "key": "sc", "style": "emphasis", "hotkey": "3"},
]
LINK_WORDS = {"answers": "Answers", "supports": "Supports", "contradicts": "Contradicts", "elaborates": "Elaborates",
              "same_claim": "Same claim as", "quotes": "Quotes", "cites": "Cites", "derived_from": "Derived from",
              "related": "Related to"}


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def normalize(body: str | None) -> str:
    return (body or "").replace("\r\n", "\n")


def parse_meta(body: str | None) -> dict | None:
    m = META_RE.search(normalize(body))
    return json.loads(m.group(1)) if m else None


def meta_block(meta: dict) -> str:
    # "<" / ">" only occur inside JSON strings, where \\u003c / \\u003e are equivalent; this keeps
    # the payload from ever closing the HTML comment.
    payload = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return "<!-- scythe:v1\n" + payload.replace("<", "\\u003c").replace(">", "\\u003e") + "\n-->"


def kind_of(labels: set[str]) -> str | None:
    for kind, label in KIND_LABELS.items():
        if label in labels:
            return kind
    return None


def label_names(issue: dict) -> set[str]:
    return {lb["name"] if isinstance(lb, dict) else lb for lb in issue.get("labels", [])}


def prefixed(labels: set[str], prefix: str) -> str | None:
    for name in sorted(labels):
        if name.startswith(prefix + ":"):
            return name[len(prefix) + 1:]
    return None


# ---------------------------------------------------------------- cards


def _shim(meta: dict, title: str = ""):
    """Duck-typed card for markdown.py (which renders ORM cards in the API)."""
    spans = [
        SimpleNamespace(accepted=True, start_char=s["start_char"], end_char=s["end_char"],
                        label=SimpleNamespace(style=s.get("style", "custom")))
        for s in meta.get("spans", [])
        if s.get("key", "sc") != "ents"
    ]
    return SimpleNamespace(id=None, node_id=None, source_id=meta.get("source"), url=meta.get("url"),
                           spacy_model=(meta.get("processed") or {}).get("model", ""), text=meta["text"],
                           tag=title, cite=meta.get("cite", ""), spans=spans)


def card_body(meta: dict) -> str:
    parts = []
    if meta.get("cite"):
        parts.append(cite_markdown(meta["cite"]))
    parts.append(body_markdown(_shim(meta), flavor="github"))
    links = meta.get("links") or []
    if links:
        parts.append("\n".join(f"{LINK_WORDS.get(lk['rel'], lk['rel'])} #{lk['to']}" for lk in links))
    parts.append(meta_block(meta))
    body = "\n\n".join(p for p in parts if p) + "\n"
    if len(body) > BODY_LIMIT:
        raise ValueError(f"card is too long for one issue ({len(body):,} > {BODY_LIMIT:,} characters)")
    return body


def card_labels(_meta: dict, *, topic: str | None, file: str | None, block: str | None, approved: bool) -> list[str]:
    labels = [KIND_LABELS["card"]]
    if topic:
        labels.append(f"topic:{topic}")
    if file and block:
        labels += [f"file:{file}"[:50], f"block:{block}"[:50]]
    if approved:
        labels.append("approved")
    return labels


# ---------------------------------------------------------------- sources


def source_text(body: str, comments: list[dict] | None = None) -> str:
    """Reassemble a source's verbatim text from its body and `scythe:part` comments."""
    body = normalize(body)
    m = TEXT_RE.search(body)
    parts = {1: m.group(1)} if m else {}
    for c in comments or []:
        for n, chunk in PART_RE.findall(normalize(c.get("body"))):
            parts[int(n)] = chunk
    return "".join(parts[k] for k in sorted(parts))


def split_text(text: str) -> list[str]:
    """Split on paragraph boundaries into issue-sized parts."""
    parts, cur = [], ""
    for para in re.split(r"(?<=\n\n)", text):
        if cur and len(cur) + len(para) > PART_SIZE:
            parts.append(cur)
            cur = ""
        while len(para) > PART_SIZE:
            parts.append(para[:PART_SIZE])
            para = para[PART_SIZE:]
        cur += para
    if cur or not parts:
        parts.append(cur)
    return parts


def source_body(meta: dict, first_part: str) -> str:
    return f"{meta_block(meta)}\n\n<!-- scythe:text -->\n{first_part}\n<!-- /scythe:text -->\n"


def part_comment(n: int, chunk: str) -> str:
    return f"<!-- scythe:part {n} -->\n{chunk}\n<!-- /scythe:part -->\n"


# ---------------------------------------------------------------- git files


def diffable_json(obj: dict) -> str:
    """JSON with one top-level key per line and one list item (span, link) per line, so `git diff`
    and `git log -p` show exactly which label or link changed."""
    lines = ["{"]
    items = list(obj.items())
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if isinstance(v, list) and v:
            lines.append(f"  {json.dumps(k)}: [")
            lines += [f"    {json.dumps(x, ensure_ascii=False)}{',' if j < len(v) - 1 else ''}" for j, x in enumerate(v)]
            lines.append(f"  ]{comma}")
        else:
            lines.append(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}{comma}")
    lines.append("}")
    return "\n".join(lines)


CARD_FIELDS = ("issue", "title", "topic", "file", "block", "approved", "cite", "url", "source", "links", "spans", "text")
SOURCE_FIELDS = ("issue", "title", "url", "publisher", "published", "authors", "cite", "fetched")


def _split_front(content: str) -> tuple[dict, str]:
    content = normalize(content)
    if not content.startswith("---\n"):
        raise ValueError("missing front matter")
    end = content.index("\n---\n", 4)
    return json.loads(content[4:end]), content[end + 5:]


def card_file(card: dict) -> str:
    """cards/<n>.md: front matter is the record; the Markdown below is a readable rendering."""
    head = {k: card.get(k) for k in CARD_FIELDS}
    head["links"], head["spans"] = head["links"] or [], head["spans"] or []
    shim = _shim({"text": card["text"], "cite": card.get("cite", ""), "spans": head["spans"]}, card.get("title", ""))
    return f"---\n{diffable_json(head)}\n---\n\n{card_markdown(shim)}"


def parse_card_file(content: str) -> dict:
    head, _ = _split_front(content)
    return head


def source_file(source: dict, text: str) -> str:
    """sources/<n>.md: the body after the front matter is the verbatim text, byte for byte."""
    head = {k: source.get(k) for k in SOURCE_FIELDS}
    return f"---\n{diffable_json(head)}\n---\n\n{text}\n"


def parse_source_file(content: str) -> tuple[dict, str]:
    head, body = _split_front(content)
    if not body.startswith("\n") or not body.endswith("\n"):
        raise ValueError("malformed source file")
    return head, body[1:-1]


def card_issue_meta(card: dict, derived: dict | None = None) -> dict:
    """The metadata block written into a card's issue (read by the local index and the app)."""
    meta = {"kind": "card", "path": f"cards/{card['issue']}.md", "text": card["text"], "cite": card.get("cite", ""),
            "url": card.get("url", ""), "source": card.get("source"), "topic": card.get("topic"),
            "spans": card.get("spans") or [], "links": card.get("links") or []}
    if derived:
        meta.update(derived)
    return meta

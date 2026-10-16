"""Canonical Markdown rendering of cards ("Scythe MD"). Used for the markdown display mode,
print previews, and as the text handed to chunkers.

Dialect (CommonMark + two widely supported extensions):

    #### <tag>
    **<cite head>**<rest of cite>

    plain text is unread; <u>underlined</u>; ==highlighted==; **emphasized**

Formatting nests in a fixed order — underline outermost, then highlight, then emphasis — so
`<u>==**word**==</u>` is underlined + highlighted + emphasized. Markers never touch whitespace
(leading/trailing spaces are moved outside) so every renderer parses them. Markdown-significant
characters in card text are backslash-escaped; paragraphs are separated by a blank line.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # duck-typed at runtime, so the GitHub Action can render without SQLAlchemy
    from .models import Card

STYLED = ("underline", "highlight", "emphasis")
_INLINE = re.compile(r"([\\`*_\[\]<>=~|])")
_BLOCK_START = re.compile(r"^(#+|[+-]|\d+\.)(?=\s)", re.M)  # would start a heading / list


_URL = re.compile(r"https?://[^\s<>\]\)]+")


def escape(text: str) -> str:
    """Backslash-escape inline Markdown syntax, leaving URLs intact so autolinkers keep them whole."""
    out, pos = [], 0
    for m in _URL.finditer(text):
        out.append(_INLINE.sub(r"\\\1", text[pos:m.start()]))
        out.append(m.group())
        pos = m.end()
    out.append(_INLINE.sub(r"\\\1", text[pos:]))
    return "".join(out)


def _flags(card: "Card", accepted_only: bool = True) -> list[frozenset[str]]:
    flags: list[set[str]] = [set() for _ in card.text]
    for s in card.spans:
        if (s.accepted or not accepted_only) and s.label.style in STYLED:
            for i in range(s.start_char, s.end_char):
                flags[i].add(s.label.style)
    return [frozenset(f) for f in flags]


ORDER = ("underline", "highlight", "emphasis")  # outermost -> innermost
MARKERS = {
    # Scythe MD (print preview, chunkers): <u>, ==mark==, **strong**
    "scythe": ({"underline": "<u>", "highlight": "==", "emphasis": "**"},
               {"underline": "</u>", "highlight": "==", "emphasis": "**"}),
    # GitHub issue bodies: GFM has no ==mark==, and its sanitizer keeps <ins>/<mark> but not <u>.
    "github": ({"underline": "<ins>", "highlight": "<mark>", "emphasis": "**"},
               {"underline": "</ins>", "highlight": "</mark>", "emphasis": "**"}),
}


def _segments(text: str, flags: list[frozenset[str]], lo: int, hi: int):
    i = lo
    while i < hi:
        j = i
        while j < hi and flags[j] == flags[i]:
            j += 1
        yield text[i:j], flags[i]
        i = j


def _render_paragraph(segs, condensed: bool, flavor: str = "scythe") -> str:
    """Emit properly nested markers: a style stays open across adjacent runs that share it,
    and whitespace is kept outside every opening/closing marker."""
    OPEN, CLOSE = MARKERS[flavor]
    out: list[str] = []
    stack: list[str] = []
    pending = ""  # whitespace waiting to be placed relative to the next markers

    def close_to(k: int) -> None:
        while len(stack) > k:
            out.append(CLOSE[stack.pop()])

    for text, flags in segs:
        if condensed and not flags:
            close_to(0)
            if out and out[-1] != " … ":
                out.append(" … ")
            pending = ""
            continue
        if not text.strip():
            pending += text
            continue
        core = text.strip()
        lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
        pending += lead
        target = [f for f in ORDER if f in flags]
        k = 0
        while k < min(len(stack), len(target)) and stack[k] == target[k]:
            k += 1
        close_to(k)
        if out and out[-1] == " … ":
            pending = ""
        out.append(pending)
        for f in target[k:]:
            out.append(OPEN[f])
            stack.append(f)
        out.append(escape(core))
        pending = trail
    close_to(0)
    return "".join(out).strip(" …") if condensed else "".join(out).strip()


def body_markdown(card: "Card", condensed: bool = False, flavor: str = "scythe") -> str:
    """Card body only. `condensed` keeps formatted text and collapses unread stretches to ` … `."""
    flags = _flags(card)
    paras, pos = [], 0
    for line in card.text.split("\n"):
        segs = _segments(card.text, flags, pos, pos + len(line))
        rendered = _render_paragraph(segs, condensed, flavor)
        if rendered:
            paras.append(rendered)
        pos += len(line) + 1
    md = "\n\n".join(paras)
    return _BLOCK_START.sub(_escape_block, md)


def _escape_block(m: re.Match) -> str:
    marker = m.group(1)
    return marker[:-1] + "\\." if marker.endswith(".") else "\\" + marker


def cite_markdown(cite: str) -> str:
    if not cite:
        return ""
    head, sep, rest = cite.partition(",")
    return f"**{escape(head.strip())}**{escape(sep + rest)}"


def card_markdown(card: "Card", *, condensed: bool = False, frontmatter: bool = False, heading_level: int = 4) -> str:
    parts = []
    if frontmatter:
        meta = {
            "card_id": card.id,
            "node_id": card.node_id,
            "source_id": card.source_id,
            "url": card.url or None,
            "spacy_model": card.spacy_model,
        }
        import yaml

        parts.append("---\n" + yaml.safe_dump({k: v for k, v in meta.items() if v is not None}, sort_keys=False) + "---")
    parts.append(f"{'#' * heading_level} {escape(card.tag) if card.tag else 'Untagged'}")
    if card.cite:
        parts.append(cite_markdown(card.cite))
    parts.append(body_markdown(card, condensed=condensed))
    return "\n\n".join(p for p in parts if p) + "\n"


def collection_markdown(title: str, groups: list[tuple[str | None, list["Card"]]], condensed: bool = False) -> str:
    """A file / block printout: `# title`, `### block` headings, then cards."""
    out = [f"# {escape(title)}"]
    for name, cards in groups:
        if name:
            out.append(f"### {escape(name)}")
        out.extend(card_markdown(c, condensed=condensed).rstrip() for c in cards)
    return "\n\n".join(out) + "\n"

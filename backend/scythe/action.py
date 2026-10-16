"""GitHub Action worker: mirror card and source files to their issues, with spaCy.

    python -m scythe.action sync A:cards/3.md M:cards/4.md D:cards/5.md S:sources/2.md
    python -m scythe.action resync          # every file under cards/ and sources/

Files in git are the record (the app commits them; history and divergence are plain git). This
worker only *reads* them from the checkout and rewrites each issue: title, labels, body = rendered
card + metadata with spaCy token offsets, entities and a `processed` stamp; cards become sub-issues
of their source; a deleted card file closes its issue. It never commits, so it can't diverge from
anyone's save. Stdlib + spaCy only.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .issue_doc import (
    card_body, card_issue_meta, card_labels, meta_block, parse_card_file, parse_source_file, text_sha,
)

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")  # set by GitHub Actions


class Api:
    """Minimal GitHub REST client (urllib); same method names as scythe.github.FakeGitHub."""

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "scythe-action"}

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers={**self.headers, **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 422 and path.endswith("/sub_issues"):
                return None  # already a sub-issue
            raise RuntimeError(f"GitHub {e.code} {method} {path}: {e.read()[:300]!r}") from e

    def get_issue(self, repo: str, number: int) -> dict:
        return self._req("GET", f"/repos/{repo}/issues/{number}")

    def update_issue(self, repo: str, number: int, **fields) -> dict:
        return self._req("PATCH", f"/repos/{repo}/issues/{number}", fields)

    def add_sub_issue(self, repo: str, parent: int, child_id: int) -> None:
        self._req("POST", f"/repos/{repo}/issues/{parent}/sub_issues", {"sub_issue_id": child_id})


def blob_sha(content: str) -> str:
    """Git's blob id for the file, the same `sha` the Contents API reports."""
    import hashlib

    raw = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def model_id(nlp) -> str:
    return f"{nlp.meta['lang']}_{nlp.meta['name']}-{nlp.meta['version']}"


def process(text: str, spans: list[dict], nlp) -> dict:
    """Snap spans to spaCy tokens (same rule as the API: char_span expand), add entities."""
    doc = nlp(text)
    out, seen = [], set()
    for s in spans:
        if s.get("key") == "ents":
            continue
        sp = doc.char_span(s["start_char"], s["end_char"], alignment_mode="expand")
        if sp is None or len(sp) == 0:
            continue
        key = (s["label"], s.get("key", "sc"), sp.start, sp.end)
        if key not in seen:
            seen.add(key)
            out.append({**s, "start_char": sp.start_char, "end_char": sp.end_char, "start": sp.start, "end": sp.end})
    ents = [{"label": e.label_, "key": "ents", "style": "entity", "start_char": e.start_char, "end_char": e.end_char,
             "start": e.start, "end": e.end, "src": "ner"} for e in doc.ents]
    return {"spans": out + ents, "processed": {"model": model_id(nlp), "text_sha": text_sha(text), "tokens": len(doc)}}


def sync_card(gh, repo: str, path: str, content: str | None, load_nlp: Callable) -> dict:
    n = int(Path(path).stem)
    if content is None:  # file deleted: the card is archived
        return gh.update_issue(repo, n, state="closed", state_reason="not_planned")
    card = parse_card_file(content)
    derived = process(card["text"], card.get("spans") or [], load_nlp())
    derived["processed"]["file_sha"] = blob_sha(content)  # the app compares this to the file it loaded
    topic = (card.get("topic") or {}).get("slug")
    labels = card_labels({}, topic=topic, file=card.get("file"), block=card.get("block"), approved=bool(card.get("approved")))
    issue = gh.get_issue(repo, n)
    extra = [lb["name"] for lb in issue.get("labels", [])
             if not lb["name"].startswith(("scythe:", "topic:", "file:", "block:")) and lb["name"] != "approved"]
    updated = gh.update_issue(repo, n, title=card.get("title") or card["text"][:80],
                              body=card_body(card_issue_meta(card, derived)), labels=labels + extra, state="open")
    if card.get("source"):
        gh.add_sub_issue(repo, card["source"], issue["id"])
    return updated


def sync_source(gh, repo: str, path: str, content: str | None) -> dict | None:
    if content is None:
        return None
    head, text = parse_source_file(content)
    n = int(Path(path).stem)
    meta = {"kind": "source", "path": path, "text_path": path, "text_sha": text_sha(text), "length": len(text),
            **{k: head.get(k) for k in ("title", "url", "publisher", "published", "authors", "cite")}}
    excerpt = text[:600] + ("…" if len(text) > 600 else "")
    byline = ", ".join(x for x in [", ".join(head.get("authors") or []), head.get("publisher") or "", head.get("published") or ""] if x)
    body = "\n\n".join(x for x in [byline, head.get("url") or "", f"> {excerpt}" if excerpt else "",
                                   f"Full text: [`{path}`](../blob/main/{path}), {len(text):,} characters", meta_block(meta)] if x)
    return gh.update_issue(repo, n, title=head.get("title") or head.get("url") or "Untitled source", body=body + "\n",
                           labels=["scythe:source"], state="open")


def _spacy_loader() -> Callable:
    cache = {}

    def load():
        if "nlp" not in cache:
            import spacy

            cache["nlp"] = spacy.load(os.environ.get("SCYTHE_SPACY_MODEL", "en_core_web_sm"))
        return cache["nlp"]

    return load


def run(gh, repo: str, changes: list[tuple[str, str]], root: Path, load_nlp: Callable) -> list[str]:
    """changes: (git status letter, path). Sources first, so new cards can attach under them."""
    done = []
    for status, path in sorted(changes, key=lambda c: not c[1].startswith("sources/")):
        if not path.endswith(".md") or not Path(path).stem.isdigit():
            continue
        # Read the latest version (the workflow checks out origin/main); gone since = deleted.
        file = root / path
        content = file.read_text() if not status.startswith("D") and file.exists() else None
        if path.startswith("cards/"):
            sync_card(gh, repo, path, content, load_nlp)
        elif path.startswith("sources/"):
            sync_source(gh, repo, path, content)
        else:
            continue
        done.append(f"{status} {path}")
    return done


def main(argv: list[str]) -> None:
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    root = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    if argv[:1] == ["resync"]:
        changes = [("M", p.relative_to(root).as_posix()) for d in ("sources", "cards") for p in sorted((root / d).glob("*.md"))]
    else:
        changes = [tuple(a.split(":", 1)) for a in argv[1:]]
    for line in run(Api(token), repo, changes, root, _spacy_loader()):
        print(line)


if __name__ == "__main__":
    main(sys.argv[1:])

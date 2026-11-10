"""Splits a help document into sections by its numbered headings (## 4 Cancellations, ### 4.2 Refunds)."""

import hashlib
import json
import re

HEADING = re.compile(r"^(#{2,4})\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$")
TITLE = re.compile(r"^#\s+(.+?)\s*$")
MAX_WORDS = 300
OVERLAP_PARAGRAPHS = 1


def parse_front_matter(text: str) -> tuple[dict, str]:
    """`key: value` lines between --- fences. `route` holds a JSON list of [lng, lat] points."""
    meta = {}
    if text.startswith("---"):
        head, _, text = text[3:].partition("\n---")
        for line in head.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        text = text.lstrip("\n")
    if "route" in meta:
        meta["route"] = json.loads(meta["route"])
    return meta, text


def split_sections(markdown: str, doc_title: str = "") -> list[dict]:
    """One dict per numbered section. Headings inside ``` code fences do not count."""
    sections, path, in_fence = [], {}, False
    current = {"section_no": "0", "heading": doc_title, "level": 1, "lines": []}
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else HEADING.match(line)
        if match is None:
            if not (TITLE.match(line) and not in_fence and not current["lines"] and current["section_no"] == "0"):
                current["lines"].append(line)
            continue
        sections.append(current)
        level, number, heading = len(match.group(1)), match.group(2), match.group(3)
        path = {lvl: text for lvl, text in path.items() if lvl < level}
        path[level] = f"{number} {heading}"
        current = {"section_no": number, "heading": heading, "level": level, "lines": [],
                   "path": " > ".join([doc_title] + [path[lvl] for lvl in sorted(path)])}
    sections.append(current)

    out = []
    for s in sections:
        body = "\n".join(s["lines"]).strip()
        if not body:
            continue  # a heading whose text is all in its subsections
        out.append({
            "section_no": s["section_no"],
            "heading": s["heading"],
            "level": s["level"],
            "anchor": "s" + s["section_no"].replace(".", "-"),
            "section_path": s.get("path", doc_title),
            "body": body,
        })
    return out


def pieces(body: str) -> list[str]:
    """A long section splits on paragraphs, each piece starting with the last paragraph of the one before."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    out, current = [], []
    for p in paragraphs:
        if current and sum(len(x.split()) for x in current) + len(p.split()) > MAX_WORDS:
            out.append("\n\n".join(current))
            current = current[-OVERLAP_PARAGRAPHS:]
        current.append(p)
    out.append("\n\n".join(current))
    return out


def chunks(markdown: str, doc_title: str) -> list[dict]:
    out = []
    for section in split_sections(markdown, doc_title):
        for text in pieces(section["body"]):
            out.append({
                **section, "ord": len(out), "body": text,
                "token_count": round(len(text.split()) * 1.3),
                "content_hash": hashlib.sha256(f"{section['section_path']}\n{text}".encode()).hexdigest(),
            })
    return out

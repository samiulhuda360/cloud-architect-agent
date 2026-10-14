"""Read uploaded documents (PDF, Word, Markdown or plain text) into sections of sentences.

Both extractors (company policies and requirements briefs) work on this shape, so every rule or
field they find can point back to the section and the exact sentence it came from.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

SUPPORTED = (".pdf", ".docx", ".md", ".markdown", ".txt")


@dataclass(frozen=True)
class Sentence:
    section: str
    text: str
    follows: tuple[str, ...] = ()  # list items that complete a sentence ending in a colon


def read_document(name: str, data: bytes) -> str:
    """The document's text, with headings marked '#' so sections survive every format."""
    ext = "." + name.lower().rsplit(".", 1)[-1] if "." in name else ".txt"
    if ext == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    if ext == ".docx":
        import docx

        out = []
        for p in docx.Document(io.BytesIO(data)).paragraphs:
            style = (p.style.name or "").lower() if p.style is not None else ""
            text = p.text.strip()
            if not text:
                continue
            if style.startswith("heading") or style == "title":
                level = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
                out.append("#" * level + " " + text)
            elif "list" in style:
                out.append("- " + text)
            else:
                out.append(text)
        return "\n\n".join(out)
    if ext not in SUPPORTED:
        raise ValueError(f"unsupported file type {ext}: use PDF, DOCX, Markdown or text")
    return data.decode("utf-8", errors="replace")


_HEADING = re.compile(r"^(#{1,6})\s+(.+)$|^((?:\d+\.)*\d+\.?)\s+([A-Z][^.!?]{2,80})$")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)]|[a-z][.)])\s+(.+)$")


def _section_label(match: re.Match) -> str:
    if match.group(2):
        title = match.group(2).strip()
        num = re.match(r"^((?:\d+\.)*\d+\.?)\s+", title)
        return (num.group(1).rstrip(".") if num else title)[:60]
    return match.group(3).rstrip(".")


def _clean(text: str) -> str:
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)  # markdown emphasis; `code` quotes stay (tag names use them)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> list[Sentence]:
    """Split into sentences, each tagged with its section. A sentence ending in ':' collects the list after it."""
    out: list[Sentence] = []
    section = ""
    para: list[str] = []
    pending: Sentence | None = None
    items: list[str] = []

    def flush_para():
        nonlocal para, pending, items
        if pending is not None:
            out.append(Sentence(pending.section, pending.text, tuple(items)))
            pending, items = None, []
        joined = _clean(" ".join(para))
        para = []
        if not joined:
            return
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", joined)
        for part in parts:
            if pending is not None:
                out.append(Sentence(pending.section, pending.text, tuple(items)))
                pending, items = None, []
            if part.endswith(":"):
                pending = Sentence(section, part)
            else:
                out.append(Sentence(section, part))

    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"[-=|: ]{3,}", line):
            if para:
                flush_para()
            continue
        b = _BULLET.match(line)
        if b and not line.startswith("#") and (pending is not None or (para and para[-1].rstrip().endswith(":"))):
            # an item of the list a sentence ending in ':' introduced, even when it is numbered like a heading
            if para:
                flush_para()
            items.append(_clean(b.group(1)))
            continue
        h = _HEADING.match(line)
        if h and not line.endswith((".", ":")):
            flush_para()
            if pending is not None:
                out.append(Sentence(pending.section, pending.text, tuple(items)))
                pending, items = None, []
            section = _section_label(h)
            continue
        if b:
            if para:
                flush_para()
            item = _clean(b.group(1))
            if pending is not None:
                items.append(item)
            else:
                out.append(Sentence(section, item))
            continue
        if line.startswith("|"):  # a table row: each cell is read as text
            cells = [_clean(c) for c in line.strip("|").split("|") if _clean(c)]
            if cells:
                flush_para()
                out.append(Sentence(section, " | ".join(cells)))
            continue
        para.append(line)
    flush_para()
    if pending is not None:
        out.append(Sentence(pending.section, pending.text, tuple(items)))
    return out


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def grounded(quote: str, text: str) -> bool:
    """Whether a quote really appears in the document (ignoring case, punctuation and spacing)."""
    q = normalise(quote)
    return bool(q) and q in normalise(text)

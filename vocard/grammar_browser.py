"""Read-only chapter navigation over the local textbook OCR draft."""

import json
import os
import re
from pathlib import Path


def load_parts(path=None):
    source = Path(
        path or os.getenv("VOCARD_GRAMMAR_PATH", "output/pdf/textbook-extraction/textbook.json")
    )
    data = json.loads(source.read_text())
    pages = data["pages"]
    starts = []
    for index, page in enumerate(pages):
        text = page["text"]
        match = re.search(r"제\s*(\d+)\s*[장잠]", text)
        # Standalone chapter dividers, not references or the table of contents.
        if match and len(text) < 180:
            starts.append((index, int(match[1]), text))
    parts = []
    if starts and starts[0][0]:
        parts.append(
            {"id": "intro", "title": "Introduction & contents", "start": 0, "end": starts[0][0]}
        )
    for i, (start, number, heading) in enumerate(starts):
        title = re.sub(r"제\s*\d+\s*[장잠]", "", heading).strip().replace("\n", " ")
        parts.append(
            {
                "id": f"part-{number}",
                "title": f"{number:02d} · {title}",
                "start": start,
                "end": starts[i + 1][0] if i + 1 < len(starts) else len(pages),
            }
        )
    if not parts:
        parts = [{"id": "textbook", "title": "Grammar textbook", "start": 0, "end": len(pages)}]
    for part in parts:
        start, end = part.pop("start"), part.pop("end")
        part["pages"] = [
            {"number": p.get("pdf_page_number", start + j + 1), "text": p["text"]}
            for j, p in enumerate(pages[start:end])
        ]
    return parts


def part_context(part, question):
    terms = set(re.findall(r"[\w]+", question.lower()))
    pages = sorted(
        part["pages"], key=lambda p: sum(term in p["text"].lower() for term in terms), reverse=True
    )
    excerpts = []
    remaining = 14000
    for page in pages:
        text = f"[PDF page {page['number']}]\n{page['text']}"
        if len(text) <= remaining:
            excerpts.append(text)
            remaining -= len(text)
    return "\n\n".join(excerpts)

"""Grammar-point chunks with complete original-text provenance."""

import hashlib
import json
import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from vocard.ingestion import InputRejected

LESSON = re.compile(r"^\s*(?:(?:Must|Lesson)\s*(\d+)|제\s*(\d+)\s*[과강])\b", re.I)


def lesson_heading(text):
    match = LESSON.match(text)
    if not match:
        return None, None
    number = int(match.group(1) or match.group(2))
    title = text[match.end() :].strip(" :\n\r\t")
    title = re.split(r"\n\s*[•►▶>]", title)[0].strip()
    return number, title or None


def block_text(block):
    return block if isinstance(block, str) else block.get("text", "")


def validate_context(course_id, unit_id, lesson_number, lesson_title):
    for value in (course_id, unit_id, lesson_title):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InputRejected("Course, unit and lesson title must be nonempty strings or null")
    if lesson_number is not None and (type(lesson_number) is not int or lesson_number < 1):
        raise InputRejected("Lesson number must be a positive integer or null")


def points_from_pages(pages):
    points, current = [], None
    context = {"course_id": None, "unit_id": None, "lesson_number": None, "lesson_title": None}
    for page in pages:
        meta, text = page.metadata, page.page_content
        next_context = dict(context)
        for key in ("course_id", "unit_id"):
            if meta.get(key) is not None:
                next_context[key] = meta[key]
        if any(next_context[k] != context[k] for k in ("course_id", "unit_id")):
            next_context.update(lesson_number=None, lesson_title=None)
        if meta.get("lesson_number") is not None:
            if meta["lesson_number"] != next_context["lesson_number"]:
                next_context["lesson_title"] = None
            next_context["lesson_number"] = meta["lesson_number"]
        if meta.get("lesson_title") is not None:
            next_context["lesson_title"] = meta["lesson_title"]
        headings, cursor = {}, 0
        for heading in meta.get("grammar", {}).get("headings", []):
            title = block_text(heading)
            if not title:
                continue
            position = text.find(title, cursor)
            if position >= 0:
                headings[position] = title
                cursor = position + len(title)
        starts = sorted({0, *headings})
        for start, end in zip(starts, [*starts[1:], len(text)], strict=True):
            title = headings.get(start)
            if title is not None:
                number, lesson_title = lesson_heading(title)
                if number is not None:
                    if meta.get("lesson_number") not in (None, number):
                        raise InputRejected("Lesson heading conflicts with explicit page metadata")
                    next_context["lesson_number"] = number
                    next_context["lesson_title"] = meta.get("lesson_title") or lesson_title
                elif (
                    re.match(r"^\s*(?:Must|Lesson)\b", title, re.I)
                    and meta.get("lesson_number") is None
                ):
                    # Ambiguous OCR such as "Must O1" must not inherit the previous lesson.
                    next_context.update(lesson_number=None, lesson_title=None)
            validate_context(**next_context)
            if current is None or title is not None or next_context != context:
                current = {
                    "context": dict(next_context),
                    "title": title or next_context["lesson_title"],
                    "pieces": [],
                }
                points.append(current)
            current["pieces"].append({"page": page, "start": start, "end": end})
            context = dict(next_context)
    return points


def point_text(point):
    text, positioned = "", []
    for piece in point["pieces"]:
        if positioned:
            text += "\n"  # Page joins are explicit; original characters are never replaced.
        part = piece["page"].page_content[piece["start"] : piece["end"]]
        positioned.append({**piece, "point_start": len(text), "point_end": len(text) + len(part)})
        text += part
    return text, positioned


def example_ranges(text, pieces):
    ranges = []
    for piece in pieces:
        for example in piece["page"].metadata.get("grammar", {}).get("examples", []):
            value = block_text(example)
            if not value:
                continue
            source = piece["page"].page_content
            cursor = piece["start"]
            while (start := source.find(value, cursor, piece["end"])) >= 0:
                end = start + len(value)
                for translation in (
                    example.get("translations", []) if isinstance(example, dict) else []
                ):
                    translated = block_text(translation)
                    position = source.find(translated, end, piece["end"]) if translated else -1
                    if position >= 0 and not source[end:position].strip():
                        end = position + len(translated)
                offset = piece["point_start"] - piece["start"]
                ranges.append((start + offset, end + offset))
                cursor = end
    # Imported overlapping example pairs must not create duplicated source intervals.
    merged = []
    for start, end in sorted(set(ranges)):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def packed_ranges(text, examples, size, overlap):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=0,
        strip_whitespace=False,
        keep_separator=True,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    atoms, cursor = [], 0
    for start, end in examples:
        if end - cursor <= size:
            atoms.append((cursor, end))  # Explanation + associated example whenever it fits.
        else:
            if start > cursor:
                atoms.append((cursor, start))
            atoms.append((start, end))
        cursor = end
    if cursor < len(text):
        atoms.append((cursor, len(text)))
    bounded = []
    for start, end in atoms:
        if end - start <= size:
            bounded.append((start, end))
            continue
        offset = start
        for part in splitter.split_text(text[start:end]):
            position = text.find(part, offset, end)
            if position != offset:
                raise InputRejected("Grammar chunk splitting dropped source characters")
            bounded.append((position, position + len(part)))
            offset = position + len(part)
        if offset != end:
            raise InputRejected("Grammar chunk splitting dropped trailing text")
    packed = []
    for start, end in bounded:
        if packed and end - packed[-1][0] <= size:
            packed[-1] = (packed[-1][0], end)
        else:
            # Overlap only whole atoms, so context does not cut through a small example.
            previous = next(
                ((a, b) for a, b in reversed(bounded) if b == start and b - a <= overlap), None
            )
            if previous and end - previous[0] <= size:
                start = previous[0]
            packed.append((start, end))
    return packed


def grammar_for_pieces(pieces):
    grammar = {
        key: [] for key in ("headings", "explanations", "examples", "translations", "example_pairs")
    }
    for piece in pieces:
        source, meta = piece["page"].page_content, piece["page"].metadata
        for key, blocks in meta.get("grammar", {}).items():
            if key not in grammar:
                continue
            for block in blocks:
                value = block_text(block) if key != "example_pairs" else ""
                belongs = (piece["start"] == 0 and piece["end"] == len(source)) or (
                    value and source.find(value, piece["start"], piece["end"]) >= 0
                )
                if belongs and block not in grammar[key]:
                    grammar[key].append(block)
    return grammar


def split_grammar_points(pages: list[Document], job: dict) -> list[Document]:
    pipeline = json.loads(job["pipeline_json"])
    size, overlap = pipeline["chunk_size"], pipeline["chunk_overlap"]
    if type(size) is not int or size < 1 or not 0 <= overlap < size:
        raise InputRejected("Invalid grammar chunk size or overlap")
    seen_pages = set()
    for page in pages:
        number = page.metadata["pdf_page_index"]
        if type(number) is not int or number < 1 or number in seen_pages:
            raise InputRejected("Grammar chunk input requires unique one-based source pages")
        if (
            page.metadata["book_id"] != job["book_id"]
            or page.metadata["book_version"] != job["book_version"]
            or page.metadata.get("sha256") != job["sha256"]
        ):
            raise InputRejected("Page belongs to another book or version")
        if seen_pages and number < max(seen_pages):
            raise InputRejected("Source pages must be in ascending PDF order")
        seen_pages.add(number)
    chunks = []
    for point in points_from_pages(pages):
        text, pieces = point_text(point)
        ranges = example_ranges(text, pieces)
        first = pieces[0]
        anchor = f"{job['sha256']}:{first['page'].metadata['pdf_page_index']}:{first['start']}"
        point_id = hashlib.sha256(anchor.encode()).hexdigest()
        grammar = grammar_for_pieces(pieces)
        for start, end in packed_ranges(text, ranges, size, overlap):
            spans, used = [], []
            for piece in pieces:
                lo, hi = max(start, piece["point_start"]), min(end, piece["point_end"])
                if hi > lo:
                    spans.append(
                        {
                            "pdf_page_index": piece["page"].metadata["pdf_page_index"],
                            "start": piece["start"] + lo - piece["point_start"],
                            "end": piece["start"] + hi - piece["point_start"],
                            "chunk_start": lo - start,
                            "chunk_end": hi - start,
                        }
                    )
                    used.append(piece["page"])
            content = text[start:end]
            if not spans:
                raise InputRejected("Chunk has no original source span")
            identity = json.dumps(
                [
                    job["book_id"],
                    job["book_version"],
                    job["pipeline_fingerprint"],
                    point_id,
                    spans,
                    content,
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
            chunk_id = hashlib.sha256(identity.encode()).hexdigest()
            meta = {
                **used[0].metadata,
                **point["context"],
                "version": job["book_version"],
                "book_id": job["book_id"],
                "book_version": job["book_version"],
                "chunk_id": chunk_id,
                "key_point_id": point_id,
                "key_point_title": point["title"],
                "source_spans": spans,
                "source_pages": sorted({s["pdf_page_index"] for s in spans}),
                "source_page_labels": [
                    {
                        "pdf_page_index": p.metadata["pdf_page_index"],
                        "printed_page_labels": p.metadata.get("printed_page_labels", []),
                    }
                    for n, p in enumerate(used)
                    if p.metadata["pdf_page_index"]
                    not in {prev.metadata["pdf_page_index"] for prev in used[:n]}
                ],
                "pdf_page_index": spans[0]["pdf_page_index"],
                "pdf_page_number": spans[0]["pdf_page_index"],
                "start_index": spans[0]["start"],
                "grammar": grammar,
                "uncertain": any(p.metadata.get("uncertain") for p in used),
                "uncertainty_flags": list(
                    dict.fromkeys(f for p in used for f in p.metadata.get("uncertainty_flags", []))
                ),
                "chunking_schema": "grammar_key_point_v1",
                "oversized_key_point": len(text) > size,
                "example_split": any(a < start < b or a < end < b for a, b in ranges),
            }
            chunks.append(Document(id=chunk_id, page_content=content, metadata=meta))
    if not 0 < len(chunks) <= pipeline["max_chunks"]:
        raise InputRejected("Chunk count is zero or exceeds the configured limit")
    validate_source_coverage(pages, chunks)
    return chunks


def validate_source_coverage(pages, chunks):
    sources = {p.metadata["pdf_page_index"]: p.page_content for p in pages}
    intervals = {number: [] for number in sources}
    for chunk in chunks:
        for span in chunk.metadata["source_spans"]:
            number, start, end = span["pdf_page_index"], span["start"], span["end"]
            if (
                chunk.page_content[span["chunk_start"] : span["chunk_end"]]
                != sources[number][start:end]
            ):
                raise InputRejected("Chunk text changed an original source span")
            intervals[number].append((start, end))
    for number, spans in intervals.items():
        covered = 0
        for start, end in sorted(spans):
            if start > covered:
                raise InputRejected("Grammar chunks dropped source text")
            covered = max(covered, end)
        if covered != len(sources[number]):
            raise InputRejected("Grammar chunks dropped a source page or trailing text")

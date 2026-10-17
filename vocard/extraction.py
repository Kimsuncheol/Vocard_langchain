"""Versioned grammar extraction, legacy import normalization and an explicit review gate."""

import copy
import hashlib
import io
import json
import re
import unicodedata
from typing import Literal

from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from pypdf import PdfReader

from vocard.ingestion import InputRejected
from vocard.ocr import TesseractOCR


class GrammarExample(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str = ""
    is_correct: StrictBool | None = None
    translations: list[str | dict] = Field(default_factory=list)
    correct_incorrect_markers: list[dict] = Field(default_factory=list)


class GrammarPage(BaseModel):
    model_config = ConfigDict(extra="allow")
    pdf_page_index: int = Field(ge=1, strict=True)
    pdf_page_number: int = Field(ge=1, strict=True)
    printed_page_labels: list[str | dict] = Field(default_factory=list)
    text: str
    headings: list[str | dict] = Field(default_factory=list)
    explanations: list[str | dict] = Field(default_factory=list)
    examples: list[GrammarExample] = Field(default_factory=list)
    translations: list[str | dict] = Field(default_factory=list)
    example_pairs: list[dict] = Field(default_factory=list)
    lines: list[dict] = Field(default_factory=list)
    uncertainty_flags: list[str] = Field(default_factory=list)
    uncertain: StrictBool = False
    course_id: str | None = Field(default=None, strict=True, min_length=1)
    unit_id: str | None = Field(default=None, strict=True, min_length=1)
    lesson_number: int | None = Field(default=None, strict=True, ge=1)
    lesson_title: str | None = Field(default=None, strict=True, min_length=1)


class ExtractionSource(BaseModel):
    model_config = ConfigDict(extra="allow")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_page_count: int = Field(gt=0)
    page_index_base: Literal[1] = 1


class GrammarExtraction(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["2.0"] = "2.0"
    source: ExtractionSource
    pages: list[GrammarPage]
    review: dict = Field(default_factory=dict)


class ReviewRequired(InputRejected):
    def __init__(self, extraction: dict):
        super().__init__(
            "Extraction requires administrator review; export and import reviewed JSON"
        )
        self.extraction = extraction


def text_flags(text: str) -> list[str]:
    if not text.strip():
        return ["empty_page"]
    flags = []
    meaningful = sum(ch.isalnum() for ch in text)
    if meaningful < 12:
        flags.append("insufficient_text")
    if "\ufffd" in text or "(cid:" in text:
        flags.append("unreadable_glyphs")
    if any(unicodedata.category(ch) == "Cc" and ch not in "\n\r\t" for ch in text):
        flags.append("control_characters")
    if meaningful / max(len(text.strip()), 1) < 0.35:
        flags.append("symbol_heavy_text")
    letters = [ch for ch in text if ch.isalpha()]
    supported = sum(bool(re.fullmatch(r"[A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ]", ch)) for ch in letters)
    if len(letters) > 10 and supported / len(letters) < 0.6:
        flags.append("unexpected_script_or_mojibake")
    if re.search(r"[A-Za-z]{60,}", text):
        flags.append("suspicious_unbroken_token")
    return flags


MARKER = re.compile(r"^\s*(?:[（(\[]\s*)?([○◯〇OXox×✕✓✔✗✘])(?:\s*[）)\]])?\s+(.*)$")


def structure_text(text: str) -> dict:
    """Conservative parsing: retain every source line and never repair grammar."""
    headings, explanations, examples, translations = [], [], [], []
    current = None
    for line in text.splitlines():
        if not line.strip():
            current = None
            continue
        marker = MARKER.match(line)
        labeled = re.match(r"^\s*예문\s+(.+)$", line)
        if marker or labeled:
            raw = marker.group(1) if marker else None
            english = marker.group(2) if marker else labeled.group(1)
            # Only explicit markers carry correctness. A check in prose is not a grammar judgment.
            correct = (
                True
                if raw in ("○", "◯", "〇", "O", "o")
                else False
                if raw in ("X", "x", "×", "✕")
                else None
            )
            korean = re.search(r"[가-힣]", english)
            inline_translation = None
            if korean and re.search(r"[A-Za-z]", english[: korean.start()]):
                inline_translation = english[korean.start() :]
                english = english[: korean.start()]
            current = {
                "text": line,
                "english": english,
                "is_correct": correct,
                "correct_incorrect_markers": ([{"raw": raw}] if raw else []),
                "translations": [inline_translation] if inline_translation else [],
                "structure_uncertain": True,
            }
            examples.append(current)
            if inline_translation:
                translations.append(inline_translation)
        elif current is not None and re.search(r"[가-힣]", line):
            current["translations"].append(line)
            translations.append(line)
        elif re.match(r"^\s*(?:Must\s*\d+|[■▪]|제\s*\d+\s*장)", line):
            headings.append({"text": line})
            current = None
        else:
            explanations.append({"text": line})
            current = None
    return {
        "headings": headings,
        "explanations": explanations,
        "examples": examples,
        "translations": translations,
    }


def normalize_import(sidecar: bytes, sha: str, count: int) -> dict:
    try:
        data = json.loads(sidecar)
        if data["source"]["sha256"] != sha:
            raise InputRejected("Extraction JSON belongs to a different PDF SHA-256")
        page_count = data["source"].get("pdf_page_count", count)
        if type(page_count) is not int or page_count != count:
            raise InputRejected("Extraction PDF page count does not match the source")
        pages = data["pages"]
        if not isinstance(pages, list) or len(pages) != count:
            raise InputRejected("Extraction must cover every PDF page exactly once, in order")
        indexes = [page["pdf_page_index"] for page in pages]
        if any(type(index) is not int for index in indexes):
            raise InputRejected("PDF page indexes must be integers")
        base = data["source"].get("page_index_base", indexes[0])
        if (
            type(base) is not int
            or base not in (0, 1)
            or indexes != list(range(base, count + base))
        ):
            raise InputRejected("Extraction must cover every PDF page exactly once, in order")
        result = copy.deepcopy(data)
        for number, page in enumerate(result["pages"], 1):
            page_number = page.get("pdf_page_number", number)
            if type(page_number) is not int or page_number != number:
                raise InputRejected("PDF page number is inconsistent with the PDF page index")
            page["source_pdf_page_index"] = page.get(
                "source_pdf_page_index", page["pdf_page_index"]
            )
            page["source_page_index_base"] = page.get("source_page_index_base", base)
            page["pdf_page_index"] = page["pdf_page_number"] = number
            if not isinstance(page.get("text"), str):
                raise InputRejected("Every extraction page must have a text string")
            page.setdefault("extraction_method", "imported_json")
            for example in page.get("examples", []):
                if "is_correct" not in example:
                    values = {
                        m.get("interpreted_value")
                        for m in example.get("correct_incorrect_markers", [])
                        if not m.get("uncertain")
                    }
                    example["is_correct"] = (
                        True
                        if values == {"correct"}
                        else False
                        if values == {"incorrect"}
                        else None
                    )
        result["schema_version"] = "2.0"
        result["source"].update({"page_index_base": 1, "pdf_page_count": count})
        # Embedded claims of approval are never trusted; the authenticated submission owns review.
        result["review"] = {}
        return GrammarExtraction.model_validate(result).model_dump()
    except InputRejected:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, ValidationError) as exc:
        raise InputRejected("Malformed structured extraction JSON") from exc


def extract_grammar(pdf: bytes, job: dict, sidecar: bytes | None, ocr=None) -> dict:
    pipeline = json.loads(job["pipeline_json"])
    if hashlib.sha256(pdf).hexdigest() != job["sha256"]:
        raise InputRejected("Source PDF SHA-256 does not match book version")
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            raise InputRejected("Encrypted PDFs are not supported")
        count = len(reader.pages)
    except InputRejected:
        raise
    except Exception as exc:
        raise InputRejected("Source is not a readable PDF") from exc
    if not 0 < count <= pipeline["max_pages"]:
        raise InputRejected("PDF page count exceeds the configured limit or is zero")
    if sidecar is not None:
        result = normalize_import(sidecar, job["sha256"], count)
    else:
        result = {
            "schema_version": "2.0",
            "source": {"sha256": job["sha256"], "pdf_page_count": count, "page_index_base": 1},
            "pages": [],
        }
        ocr = ocr or TesseractOCR()
        for index, page in enumerate(reader.pages, 1):
            flags, details = [], {}
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
                flags.append("embedded_text_extraction_failed")
            method = "pdf_embedded_text"
            try:
                resources = page.get("/Resources")
                resources = resources.get_object() if resources else {}
                objects = resources.get("/XObject")
                objects = objects.get_object() if objects else {}
                image_heavy_short_text = len(text.strip()) < 200 and any(
                    obj.get_object().get("/Subtype") == "/Image" for obj in objects.values()
                )
            except Exception:
                flags.append("image_resource_inspection_failed")
                image_heavy_short_text = True
            if text_flags(text) or image_heavy_short_text:
                native_text = text
                try:
                    details = ocr(pdf, index - 1, pipeline.get("ocr", {}))
                    text = details["text"]
                    method = "tesseract_kor_eng"
                    flags.append("ocr_requires_review")
                    if details.get("ocr_confidence", 0) < pipeline.get("ocr", {}).get(
                        "min_confidence", 85
                    ):
                        flags.append("low_ocr_confidence")
                    if text_flags(native_text) and native_text.strip():
                        details["embedded_text_before_ocr"] = native_text
                except Exception as exc:
                    flags.append("ocr_failed")
                    details = {"ocr_error_type": type(exc).__name__}
            flags.extend(text_flags(text))
            result["pages"].append(
                {
                    **details,
                    **structure_text(text),
                    "pdf_page_index": index,
                    "pdf_page_number": index,
                    "printed_page_labels": printed_labels(text),
                    "text": text,
                    "extraction_method": method,
                    "uncertain": bool(flags),
                    "uncertainty_flags": list(dict.fromkeys(flags)),
                }
            )
        result = GrammarExtraction.model_validate(result).model_dump()
    reasons = []
    reviewed = bool(
        sidecar is not None and pipeline.get("reviewed_extraction") and job.get("reviewed_by")
    )
    document_uncertain = bool(result.get("extraction", {}).get("uncertain"))
    for page in result["pages"]:
        flags = list(dict.fromkeys([*page["uncertainty_flags"], *text_flags(page["text"])]))
        if (
            document_uncertain
            or page["uncertain"]
            or any(line.get("uncertain") for line in page["lines"])
        ):
            flags.append("source_uncertainty")
        for example in page["examples"]:
            if example.get("uncertain") or example.get("pairing_uncertain"):
                flags.append("example_uncertainty")
            marker_values = {
                m.get("interpreted_value") for m in example.get("correct_incorrect_markers", [])
            }
            correct = example["is_correct"]
            if (
                correct is True
                and "incorrect" in marker_values
                or correct is False
                and "correct" in marker_values
            ):
                flags.append("conflicting_correctness_markers")
        flags = list(dict.fromkeys(flags))
        page["uncertainty_flags"] = flags
        page["uncertain"] = bool(flags)
        empty = not page["text"].strip()
        accepted_blank = reviewed and page.get("review_disposition") == "blank"
        if flags and (not reviewed or (empty and not accepted_blank)):
            reasons.append({"pdf_page_index": page["pdf_page_index"], "flags": flags})
    result["review"] = {
        "required": bool(reasons),
        "pages": reasons,
        "reviewed_by": job.get("reviewed_by") if reviewed else None,
        "reviewed_at": job.get("reviewed_at") if reviewed else None,
    }
    if not any(page["text"].strip() for page in result["pages"]):
        result["review"]["required"] = True
        result["review"]["pages"].append({"pdf_page_index": 1, "flags": ["no_indexable_text"]})
    return result


def printed_labels(text: str) -> list[dict]:
    """Capture only isolated first/last-line labels; never use them as PDF indexes."""
    lines = [line for line in text.splitlines() if line.strip()]
    labels = []
    for line in dict.fromkeys(lines[:1] + lines[-1:]):
        if re.fullmatch(r"\s*(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,10})\s*", line):
            labels.append({"text": line, "uncertain": True, "origin": "isolated_edge_line"})
    return labels


def extraction_documents(result: dict, job: dict) -> list[Document]:
    if result["review"]["required"]:
        raise ReviewRequired(result)
    documents = []
    pipeline = json.loads(job["pipeline_json"])
    for page in result["pages"]:
        if not page["text"].strip():
            continue
        documents.append(
            Document(
                page_content=page["text"],
                metadata={
                    "book_id": job["book_id"],
                    "book_version": job["book_version"],
                    "sha256": job["sha256"],
                    "pdf_page_index": page["pdf_page_index"],
                    "pdf_page_number": page["pdf_page_number"],
                    "printed_page_labels": page["printed_page_labels"],
                    "uncertain": page["uncertain"],
                    "uncertainty_flags": page["uncertainty_flags"],
                    "source_type": page["extraction_method"],
                    "pipeline_fingerprint": job["pipeline_fingerprint"],
                    "course_id": page.get("course_id")
                    or result.get("course_id")
                    or pipeline.get("course_id"),
                    "unit_id": page.get("unit_id")
                    or result.get("unit_id")
                    or pipeline.get("unit_id"),
                    "lesson_number": page.get("lesson_number"),
                    "lesson_title": page.get("lesson_title"),
                    "grammar": {
                        key: page[key]
                        for key in (
                            "headings",
                            "explanations",
                            "examples",
                            "translations",
                            "example_pairs",
                        )
                    },
                    "reviewed_by": result["review"]["reviewed_by"],
                },
            )
        )
    if not documents:
        raise InputRejected("No indexable text; an entirely empty extraction cannot publish")
    return documents


def extract_pages(pdf: bytes, job: dict, sidecar: bytes | None, ocr=None) -> list[Document]:
    return extraction_documents(extract_grammar(pdf, job, sidecar, ocr), job)

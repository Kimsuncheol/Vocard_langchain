"""Firebase student routes for the existing Render/Ollama chat deployment.

Uses the same DTOs and provenance validators as the indexed API, with a bundled
OCR snapshot for grammar and explicitly generated vocabulary explanations.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from langchain_core.documents import Document
from pydantic import ValidationError

from vocard.answering import SYSTEM_PROMPT, AnswerDraft, AnsweringPipeline, AnswerResponse
from vocard.db import Repository
from vocard.student_api import StudentQuestion
from vocard.student_auth import (
    FirebaseRestTokenVerifier,
    FirebaseTokenVerifier,
    student_dependency,
)
from vocard.usage import UsageCounters
from vocard.vocabulary import (
    VOCABULARY_COURSES,
    VOCABULARY_PROMPT,
    TutorDraft,
    TutorResponse,
    VocabularyQuestion,
    VocabularyTutor,
)


async def structured_answer(model_factory, schema, messages, resolve):
    """Validate before returning; a single schema repair stays request-local."""
    prompt = messages[0][1] + (
        "\nReturn only valid JSON matching this exact schema. Do not wrap in Markdown.\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    for attempt in range(2):
        try:
            result = await model_factory().ainvoke(
                [
                    (
                        "system",
                        prompt
                        + (
                            "\nYour previous output failed validation. Check required "
                            "fields, Korean explanations and exact source quotes."
                            if attempt
                            else ""
                        ),
                    ),
                    messages[1],
                ]
            )
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.text.strip())
            return resolve(schema.model_validate_json(text))
        except (ValidationError, ValueError):
            if attempt:
                raise HTTPException(502, "Generated answer failed validation") from None
        except httpx.HTTPStatusError as exc:
            code = 429 if exc.response.status_code == 429 else 503
            raise HTTPException(code, "AI provider is unavailable") from None
        except Exception:
            raise HTTPException(503, "AI service unavailable") from None


def grammar_documents(request, grammar_path=None, manifest_path=None):
    source = Path(
        grammar_path
        or os.getenv("VOCARD_GRAMMAR_PATH", "output/pdf/textbook-extraction/textbook.json")
    )
    manifest = Path(manifest_path or Path(__file__).with_name("mobile_lessons.json"))
    raw = source.read_bytes()
    data, mapping = json.loads(raw), json.loads(manifest.read_text())
    if data["source"]["sha256"] != mapping["source_sha256"]:
        raise ValueError("Lesson map and OCR source differ")
    course = request.course_id or "devils_100"
    if course != mapping["course_id"]:
        raise HTTPException(403, "Course is not available")
    lessons = [
        lesson
        for lesson in mapping["lessons"]
        if (request.lesson_number is None or lesson["lesson_number"] == request.lesson_number)
        and (request.unit_id is None or lesson["unit_id"] == request.unit_id)
    ]
    if not lessons:
        raise HTTPException(404, "Lesson is not available")
    page_numbers = {n for lesson in lessons for n in lesson["source_pages"]}
    pages = [p for p in data["pages"] if p["pdf_page_number"] in page_numbers]
    terms = set(re.findall(r"\w+", request.question.lower()))
    pages.sort(key=lambda p: sum(t in p["text"].lower() for t in terms), reverse=True)
    version = "bundled-ocr-" + hashlib.sha256(raw).hexdigest()[:16]
    docs, remaining = {}, 14000
    for page in pages:
        text, number = page["text"], page["pdf_page_number"]
        if not text.strip() or len(text) > remaining:
            continue
        chunk_id = f"ocr-p{number}"
        docs[chunk_id] = Document(
            id=chunk_id,
            page_content=text,
            metadata={
                "book_id": "devils-100-ocr-draft",
                "version": 1,
                "index_version": version,
                "source_pages": [number],
                "source_page_labels": [],
                "course_id": course,
                "unit_id": request.unit_id,
                "lesson_number": request.lesson_number,
                "source_examples": [],
            },
        )
        remaining -= len(text)
    return docs


def mobile_router(model_factory, verifier=None, usage=None, grammar_path=None, manifest_path=None):
    router = APIRouter(prefix="/ai")
    project = os.getenv("VOCARD_FIREBASE_PROJECT_ID")
    api_key = os.getenv("VOCARD_FIREBASE_WEB_API_KEY")
    verifier = verifier or (
        FirebaseRestTokenVerifier(project, api_key) if api_key else FirebaseTokenVerifier(project)
    )
    uid_dependency = student_dependency(verifier)
    if usage is None:
        usage = UsageCounters(
            Repository(Path(os.getenv("VOCARD_MOBILE_DB_PATH", "state/mobile.sqlite3"))),
            SimpleNamespace(ai_daily_quota=int(os.getenv("VOCARD_AI_DAILY_QUOTA", "50"))),
        )

    @router.post("/vocabulary/explain", response_model=TutorResponse)
    async def explain(request: VocabularyQuestion, uid: Annotated[str, Depends(uid_dependency)]):
        if request.selected_entry.courseId not in VOCABULARY_COURSES:
            raise HTTPException(403, "Vocabulary course is not available")
        envelope = {**request.model_dump(), "evidence": []}
        if len(json.dumps(envelope, ensure_ascii=False)) > 60000:
            raise HTTPException(422, "Tutor context is too large")
        usage.reserve(uid)
        return await structured_answer(
            model_factory,
            TutorDraft,
            [
                ("system", VOCABULARY_PROMPT),
                ("user", json.dumps(envelope, ensure_ascii=False)),
            ],
            lambda draft: VocabularyTutor._resolve(request, draft, {}),
        )

    @router.post("/answer", response_model=AnswerResponse)
    async def answer(request: StudentQuestion, uid: Annotated[str, Depends(uid_dependency)]):
        try:
            docs = grammar_documents(request, grammar_path, manifest_path)
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "Grammar content is unavailable") from None
        usage.reserve(uid)
        envelope = {
            "question": request.question,
            "language": "ko",
            "include_practice_examples": False,
            "evidence": [
                {"chunk_id": doc.id, "text": doc.page_content, "source_examples": []}
                for doc in docs.values()
            ],
        }
        return await structured_answer(
            model_factory,
            AnswerDraft,
            [
                (
                    "system",
                    SYSTEM_PROMPT + "\nThese are unreviewed OCR excerpts. "
                    "Mention OCR uncertainty where relevant. source_examples and practice_examples "
                    "must be empty because no verified example pairs were supplied.",
                ),
                ("user", json.dumps(envelope, ensure_ascii=False)),
            ],
            lambda draft: AnsweringPipeline._validate(draft, docs, "ko", False),
        )

    return router

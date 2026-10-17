"""Independent Firebase-authenticated questions; only quota metadata is stored."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from vocard.answering import (
    AnsweringPipeline,
    AnswerResponse,
    GenerationUnavailable,
    InvalidAnswer,
)
from vocard.retrieval import ActiveBookRetriever
from vocard.student_auth import FirebaseTokenVerifier, student_dependency
from vocard.usage import UsageCounters


class StudentQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    course_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"\S")
    unit_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"\S")
    lesson_number: int | None = Field(default=None, ge=1, strict=True)


def student_router(settings, repository, storage, embedding_factory, model_factory, verifier=None):
    router = APIRouter(prefix="/ai")
    usage = UsageCounters(repository, settings)
    verified_uid = student_dependency(
        verifier or FirebaseTokenVerifier(settings.firebase_project_id)
    )

    @router.post("/answer", response_model=AnswerResponse)
    def answer(request: StudentQuestion, uid: Annotated[str, Depends(verified_uid)]):
        if request.course_id is not None:
            book_id = settings.course_books.get(request.course_id)
            if book_id is None:
                raise HTTPException(403, "Course is not available")
        else:
            books = set(settings.course_books.values())
            if not books:
                raise HTTPException(403, "No book is available")
            if len(books) != 1:
                usage.reserve(uid)
                return AnswerResponse(
                    answer="어떤 강좌의 교재에 관한 질문인가요?", status="clarification_needed"
                )
            book_id = next(iter(books))
        retriever = ActiveBookRetriever(
            repository=repository,
            storage=storage,
            embedding_factory=embedding_factory,
            book_id=book_id,
            authorized_book_ids=frozenset({book_id}),
            course_id=request.course_id,
            unit_id=request.unit_id,
            current_lesson=request.lesson_number,
            top_k=settings.top_k,
            max_fallback=settings.max_fallback,
            min_score=settings.min_score,
        )
        usage.reserve(uid)
        try:
            return AnsweringPipeline(retriever, settings, model_factory).invoke(request.question)
        except KeyError as exc:
            raise HTTPException(404, "Course has no active book index") from exc
        except InvalidAnswer as exc:
            raise HTTPException(502, "Generated answer failed validation") from exc
        except GenerationUnavailable as exc:
            raise HTTPException(503, "Answer generation is unavailable") from exc

    return router

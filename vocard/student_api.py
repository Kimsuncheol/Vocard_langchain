"""Lesson answering for Firebase users. No client-provided UID, book or history."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from vocard.answering import (
    AnsweringPipeline,
    AnswerResponse,
    GenerationUnavailable,
    InvalidAnswer,
    insufficient,
)
from vocard.conversations import ConversationStore
from vocard.retrieval import ActiveBookRetriever
from vocard.student_auth import FirebaseTokenVerifier, student_dependency


class StudentQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    conversation_id: UUID | None = None
    course_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    unit_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    lesson_number: int = Field(ge=1, strict=True)


class StudentAnswer(AnswerResponse):
    conversation_id: str


def student_router(settings, repository, storage, embedding_factory, model_factory, verifier=None):
    router = APIRouter(prefix="/ai")
    store = ConversationStore(repository, settings)
    verified_uid = student_dependency(
        verifier or FirebaseTokenVerifier(settings.firebase_project_id)
    )

    @router.get("/conversations")
    def latest(
        uid: Annotated[str, Depends(verified_uid)],
        course_id: Annotated[str, Query(min_length=1, max_length=100)],
        unit_id: Annotated[str, Query(min_length=1, max_length=100)],
        lesson_number: Annotated[int, Query(ge=1)],
    ):
        book_id = settings.course_books.get(course_id)
        if book_id is None:
            raise HTTPException(403, "Course is not available")
        return {"conversation": store.latest(uid, book_id, course_id, unit_id, lesson_number)}

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: UUID, uid: Annotated[str, Depends(verified_uid)]):
        result = store.get(str(conversation_id), uid)
        if settings.course_books.get(result["course_id"]) != result["book_id"]:
            raise HTTPException(403, "Course is not available")
        return result

    @router.post("/answer", response_model=StudentAnswer)
    def answer(request: StudentQuestion, uid: Annotated[str, Depends(verified_uid)]):
        book_id = settings.course_books.get(request.course_id)
        if book_id is None:
            raise HTTPException(403, "Course is not available")
        scope = {
            "book_id": book_id,
            "course_id": request.course_id,
            "unit_id": request.unit_id,
            "lesson_number": request.lesson_number,
        }
        # Check that the requested lesson exists before reserving quota or creating history.
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
        try:
            lesson = retriever.lookup_lesson(lesson_number=request.lesson_number)
        except KeyError as exc:
            raise HTTPException(404, "Course has no active book index") from exc
        cid, lease, turns = store.begin(
            str(request.conversation_id) if request.conversation_id else None, uid, scope
        )
        try:
            history = [
                message
                for turn in turns
                for message in (
                    {"role": "user", "content": turn["question"]},
                    # History cannot become evidence: prior citations/quotes are never replayed.
                    {"role": "assistant", "content": turn["response"]["answer"][:10000]},
                )
            ]
            # Deterministic query expansion resolves short follow-ups without reusing evidence.
            retrieval_query = "\n".join([t["question"] for t in turns[-2:]] + [request.question])
            result = (
                AnsweringPipeline(retriever, settings, model_factory).invoke(
                    request.question,
                    history=history,
                    retrieval_query=retrieval_query,
                )
                if lesson
                else insufficient()
            )
            response = StudentAnswer(**result.model_dump(), conversation_id=cid)
            store.complete(
                cid,
                uid,
                lease,
                turns + [{"question": request.question, "response": result.model_dump()}],
            )
            return response
        except InvalidAnswer as exc:
            raise HTTPException(502, "Generated answer failed validation") from exc
        except GenerationUnavailable as exc:
            raise HTTPException(503, "Answer generation is unavailable") from exc
        finally:
            store.release(cid, uid, lease)

    return router

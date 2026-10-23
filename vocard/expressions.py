"""Published daily-expression content, independent of AI generation and progress."""

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from vocard.student_auth import (
    FirebaseRestTokenVerifier,
    FirebaseTokenVerifier,
    student_dependency,
)


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DialogueLine(ContentModel):
    speaker: str = Field(min_length=1)
    english: str = Field(min_length=1)
    korean: str = Field(min_length=1)


class Expression(ContentModel):
    expression_id: str = Field(min_length=1)
    english: str = Field(min_length=1)
    korean: str = Field(min_length=1)
    usage_note_ko: str | None = None
    raw_dialogue: str | None = None
    raw_translation: str | None = None
    vocabulary_notes: str | None = None
    dialogue: list[DialogueLine]
    audio_url: HttpUrl | None

    @model_validator(mode="after")
    def secure_audio(self):
        if self.audio_url and (
            self.audio_url.scheme != "https" or self.audio_url.username or self.audio_url.password
        ):
            raise ValueError("Audio must be an HTTPS resource without credentials")
        return self


class PublishedLesson(ContentModel):
    lesson_id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_-]+$")
    date: date
    title: str = Field(min_length=1)
    status: Literal["draft", "published"]
    source_url: HttpUrl | None = None
    source_name: str | None = None
    source_credit: str | None = None
    transcription_notice_ko: str | None = None
    featured_expression_id: str = Field(min_length=1)
    expressions: list[Expression] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_feature(self):
        ids = [expression.expression_id for expression in self.expressions]
        if len(set(ids)) != len(ids) or self.featured_expression_id not in ids:
            raise ValueError("Invalid featured expression or duplicate expression IDs")
        return self


def catalog(path):
    lessons = [PublishedLesson.model_validate(item) for item in json.loads(path.read_text())]
    ids = [lesson.lesson_id for lesson in lessons]
    dates = [lesson.date for lesson in lessons if lesson.status == "published"]
    if len(set(ids)) != len(ids) or len(set(dates)) != len(dates):
        raise ValueError("Lesson IDs and published dates must be unique")
    return lessons


ERRORS = {
    400: ("invalid_request", "Invalid timezone, date, or limit."),
    401: ("authentication_required", "Sign in with a valid Firebase ID token."),
    403: ("account_not_eligible", "Sign in with a non-anonymous account."),
    404: ("lesson_not_found", "No published expression lesson is available for this date."),
    503: ("content_unavailable", "Expression content is temporarily unavailable."),
}


class ExpressionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                response = await handler(request)
            except (HTTPException, RequestValidationError) as exc:
                status = 400 if isinstance(exc, RequestValidationError) else exc.status_code
                if status not in ERRORS:
                    status = 503
                code, message = ERRORS[status]
                response = JSONResponse(
                    {"error": {"code": code, "message": message}}, status_code=status
                )
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe_handler


def expressions_router(verifier=None, content_path=None, now=None, source=None):
    project = os.getenv("VOCARD_FIREBASE_PROJECT_ID")
    api_key = os.getenv("VOCARD_FIREBASE_WEB_API_KEY")
    verifier = verifier or (
        FirebaseRestTokenVerifier(project, api_key) if api_key else FirebaseTokenVerifier(project)
    )
    router = APIRouter(
        dependencies=[Depends(student_dependency(verifier))], route_class=ExpressionRoute
    )
    # File catalogs are explicit test fixtures only; production reads the scraper.
    if content_path is not None:

        class FixtureSource:
            def index(self):
                return [item for item in catalog(Path(content_path)) if item.status == "published"]

            def lesson(self, item):
                return item.model_dump(mode="json")

        source = FixtureSource()
    elif source is None:
        from vocard.expression_source import ScraperLessonSource

        source = ScraperLessonSource(
            os.getenv("VOCARD_EXPRESSION_SOURCE_URL", "https://english-scrapper.onrender.com")
        )

    def read_lesson(item):
        try:
            data = source.lesson(item)
            return PublishedLesson.model_validate(data) if data is not None else None
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, "Expression source unavailable") from exc

    def accessible(timezone):
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(400, "Invalid timezone") from exc
        today = (now() if now else datetime.now().astimezone()).astimezone(zone).date()
        try:
            lessons = source.index()
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, "Content unavailable") from exc
        return today, sorted(
            (lesson for lesson in lessons if lesson.date <= today),
            key=lambda lesson: lesson.date,
        )

    def preview(expression):
        return expression.model_dump(mode="json", include={"expression_id", "english", "korean"})

    def featured(lesson):
        return next(
            item
            for item in lesson.expressions
            if item.expression_id == lesson.featured_expression_id
        )

    @router.get("/expressions/today")
    def today_expression(timezone: str = "Asia/Seoul"):
        today, lessons = accessible(timezone)
        lesson = next((item for item in lessons if item.date == today), None)
        if lesson is None:
            raise HTTPException(404, "No lesson today")
        lesson = read_lesson(lesson)
        if lesson is None:
            raise HTTPException(503, "Today transcription incomplete")
        return {
            "date": lesson.date.isoformat(),
            "timezone": timezone,
            "lesson_id": lesson.lesson_id,
            **preview(featured(lesson)),
        }

    @router.get("/expression-lessons")
    def list_lessons(
        timezone: str = "Asia/Seoul",
        before: str | None = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ):
        _, lessons = accessible(timezone)
        boundary = None
        if before is not None:
            try:
                boundary = date.fromisoformat(before)
                if boundary.isoformat() != before:
                    raise ValueError("Noncanonical date")
            except ValueError as exc:
                raise HTTPException(400, "Invalid date") from exc
        eligible = [
            lesson for lesson in reversed(lessons) if boundary is None or lesson.date < boundary
        ]
        available = []
        for item in eligible:
            lesson = read_lesson(item)
            if lesson is not None:
                available.append(lesson)
            if len(available) > limit:
                break
        page = available[:limit]
        return {
            "items": [
                {
                    "lesson_id": lesson.lesson_id,
                    "date": lesson.date.isoformat(),
                    "title": lesson.title,
                    "expression_count": len(lesson.expressions),
                    "featured_expression": preview(featured(lesson)),
                }
                for lesson in page
            ],
            "next_before": page[-1].date.isoformat() if len(available) > limit else None,
        }

    @router.get("/expression-lessons/{lesson_id}")
    def lesson_detail(lesson_id: str, timezone: str = "Asia/Seoul"):
        _, lessons = accessible(timezone)
        index = next((i for i, item in enumerate(lessons) if item.lesson_id == lesson_id), None)
        if index is None:
            raise HTTPException(404, "No lesson")
        lesson = read_lesson(lessons[index])
        if lesson is None:
            raise HTTPException(503, "Lesson transcription incomplete")
        previous = next(
            (item.lesson_id for item in reversed(lessons[:index]) if read_lesson(item) is not None),
            None,
        )
        following = next(
            (item.lesson_id for item in lessons[index + 1 :] if read_lesson(item) is not None), None
        )
        return {
            **lesson.model_dump(mode="json", exclude={"status"}),
            "previous_lesson_id": previous,
            "next_lesson_id": following,
        }

    return router

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from vocard.auth import AdminBoundary, Administrator, administrator_dependency
from vocard.config import Settings
from vocard.db import Repository
from vocard.ingestion import IngestionService, InputRejected, validate_book_id
from vocard.pipeline import ActiveBookRetriever, embeddings_for
from vocard.retrieval import ActiveIndexChanged, BookAccessDenied
from vocard.storage import PrivateS3


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10000)
    top_k: int | None = Field(
        default=None, ge=1, le=100, strict=True, validation_alias=AliasChoices("top_k", "k")
    )
    current_lesson: int | None = Field(default=None, ge=1, strict=True)
    current_lesson_title: str | None = Field(default=None, min_length=1, pattern=r"\S")
    course_id: str | None = Field(default=None, min_length=1, pattern=r"\S")
    unit_id: str | None = Field(default=None, min_length=1, pattern=r"\S")
    allow_book_fallback: bool = True
    max_fallback: int | None = Field(default=None, ge=0, le=100, strict=True)
    min_score: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)


def create_app(settings=None, repository=None, storage=None, embedding_factory=embeddings_for):
    settings = settings or Settings.from_env()
    repository = repository or Repository(settings.db_path)
    storage = storage or PrivateS3(settings.bucket)
    service = IngestionService(settings, repository, storage)
    admin = administrator_dependency(settings)

    @asynccontextmanager
    async def lifespan(app):
        storage.assert_private()
        yield

    app = FastAPI(
        title="Vocard book ingestion",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(AdminBoundary, settings=settings)

    @app.exception_handler(InputRejected)
    async def invalid_input(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(BookAccessDenied)
    async def access_denied(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ActiveIndexChanged)
    async def active_changed(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/admin/books/{book_id}/ingestions", status_code=202)
    def ingest(
        book_id: str,
        file: Annotated[UploadFile, File()],
        actor: Annotated[Administrator, Depends(admin)],
        extraction: Annotated[UploadFile | None, File()] = None,
        allow_uncertain: Annotated[bool, Form()] = False,
        reviewed_extraction: Annotated[bool, Form()] = False,
        course_id: Annotated[str | None, Form()] = None,
        unit_id: Annotated[str | None, Form()] = None,
        chunk_size: Annotated[int | None, Form()] = None,
    ):
        actor.authorize_book(book_id)
        result = service.submit(
            book_id,
            file.file,
            actor.subject,
            extraction.file if extraction else None,
            allow_uncertain,
            reviewed_extraction,
            course_id,
            unit_id,
            chunk_size,
        )
        return {k: v for k, v in result.items() if k != "lease_token"}

    @app.get("/admin/books/{book_id}")
    def book(book_id: str, actor: Annotated[Administrator, Depends(admin)]):
        validate_book_id(book_id)
        actor.authorize_book(book_id)
        try:
            return repository.book(book_id)
        except KeyError as exc:
            raise HTTPException(404, "Book not found") from exc

    @app.get("/admin/jobs/{job_id}")
    def job(job_id: str, actor: Annotated[Administrator, Depends(admin)]):
        try:
            result = repository.job(job_id)
            actor.authorize_book(result["book_id"])
            # A worker lease is an internal capability, not part of the administrator response.
            return {k: v for k, v in result.items() if k not in ("lease_token",)}
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc

    @app.post("/admin/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, actor: Annotated[Administrator, Depends(admin)]):
        try:
            actor.authorize_book(repository.job(job_id)["book_id"])
            result = repository.retry(job_id, actor.subject)
            return {k: v for k, v in result.items() if k != "lease_token"}
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/admin/jobs/{job_id}/extraction")
    def extraction(job_id: str, actor: Annotated[Administrator, Depends(admin)]):
        from fastapi.responses import Response

        try:
            result = repository.job(job_id)
            actor.authorize_book(result["book_id"])
            if not result["extracted_key"]:
                raise KeyError(job_id)
            payload = storage.get(result["extracted_key"], result["extracted_sha256"])
            return Response(
                payload,
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="extraction.json"'},
            )
        except KeyError as exc:
            raise HTTPException(404, "Extraction is not available") from exc

    @app.post("/admin/books/{book_id}/search")
    def search(
        book_id: str, request: SearchRequest, actor: Annotated[Administrator, Depends(admin)]
    ):
        validate_book_id(book_id)
        retriever = ActiveBookRetriever(
            repository=repository,
            storage=storage,
            embedding_factory=embedding_factory,
            book_id=book_id,
            authorized_book_ids=actor.authorize_book(book_id),
            top_k=settings.top_k if request.top_k is None else request.top_k,
            current_lesson=request.current_lesson,
            current_lesson_title=request.current_lesson_title,
            course_id=request.course_id,
            unit_id=request.unit_id,
            allow_book_fallback=request.allow_book_fallback,
            max_fallback=settings.max_fallback
            if request.max_fallback is None
            else request.max_fallback,
            min_score=settings.min_score if request.min_score is None else request.min_score,
        )
        try:
            return {"documents": [d.model_dump() for d in retriever.invoke(request.query)]}
        except KeyError as exc:
            raise HTTPException(404, "Book has no active index") from exc

    @app.get("/admin/books/{book_id}/lessons")
    def lookup_lesson(
        book_id: str,
        actor: Annotated[Administrator, Depends(admin)],
        lesson_number: Annotated[int | None, Query(ge=1)] = None,
        title: Annotated[str | None, Query(min_length=1, pattern=r"\S")] = None,
        course_id: Annotated[str | None, Query(min_length=1, pattern=r"\S")] = None,
        unit_id: Annotated[str | None, Query(min_length=1, pattern=r"\S")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        validate_book_id(book_id)
        retriever = ActiveBookRetriever(
            repository=repository,
            storage=storage,
            embedding_factory=embedding_factory,
            book_id=book_id,
            authorized_book_ids=actor.authorize_book(book_id),
            course_id=course_id,
            unit_id=unit_id,
        )
        try:
            docs = retriever.lookup_lesson(lesson_number=lesson_number, title=title)
        except KeyError as exc:
            raise HTTPException(404, "Book has no active index") from exc
        return {
            "documents": [d.model_dump() for d in docs[offset : offset + limit]],
            "total_chunks": len(docs),
            "next_offset": offset + limit if offset + limit < len(docs) else None,
        }

    return app

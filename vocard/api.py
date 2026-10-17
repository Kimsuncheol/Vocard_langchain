from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from vocard.auth import AdminBoundary, Administrator, administrator_dependency
from vocard.config import Settings
from vocard.db import Repository
from vocard.ingestion import IngestionService, InputRejected, validate_book_id
from vocard.pipeline import ActiveBookRetriever, embeddings_for
from vocard.storage import PrivateS3


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    k: int = Field(default=4, ge=1, le=20)


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
    ):
        result = service.submit(
            book_id,
            file.file,
            actor.subject,
            extraction.file if extraction else None,
            allow_uncertain,
            reviewed_extraction,
        )
        return {k: v for k, v in result.items() if k != "lease_token"}

    @app.get("/admin/books/{book_id}")
    def book(book_id: str, actor: Annotated[Administrator, Depends(admin)]):
        validate_book_id(book_id)
        try:
            return repository.book(book_id)
        except KeyError as exc:
            raise HTTPException(404, "Book not found") from exc

    @app.get("/admin/jobs/{job_id}")
    def job(job_id: str, actor: Annotated[Administrator, Depends(admin)]):
        try:
            result = repository.job(job_id)
            # A worker lease is an internal capability, not part of the administrator response.
            return {k: v for k, v in result.items() if k not in ("lease_token",)}
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc

    @app.post("/admin/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, actor: Annotated[Administrator, Depends(admin)]):
        try:
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
            k=request.k,
        )
        try:
            return {"documents": [d.model_dump() for d in retriever.invoke(request.query)]}
        except KeyError as exc:
            raise HTTPException(404, "Book has no active index") from exc

    return app

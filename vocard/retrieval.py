"""Book-authorized, active-version LangChain retrieval and exact lesson lookup."""

import json
import unicodedata

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import AliasChoices, ConfigDict, Field, model_validator

from vocard.ingestion import InputRejected
from vocard.vectors import check_vector, embeddings_for, similarity


class BookAccessDenied(PermissionError):
    pass


class ActiveIndexChanged(RuntimeError):
    pass


def normalized_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFC", title).split()).casefold()


class ActiveBookRetriever(BaseRetriever):
    """Authorization is supplied by trusted server code, never inferred from a query."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    repository: object
    storage: object
    embedding_factory: object = embeddings_for
    book_id: str
    authorized_book_ids: frozenset[str]
    top_k: int = Field(
        default=4, ge=1, le=100, strict=True, validation_alias=AliasChoices("top_k", "k")
    )
    current_lesson: int | None = Field(default=None, ge=1, strict=True)
    current_lesson_title: str | None = Field(default=None, min_length=1)
    course_id: str | None = Field(default=None, min_length=1)
    unit_id: str | None = Field(default=None, min_length=1)
    allow_book_fallback: bool = True
    max_fallback: int = Field(default=2, ge=0, le=100, strict=True)
    min_score: float = Field(default=0.0, ge=-1, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def nonempty_context(self):
        for value in (self.current_lesson_title, self.course_id, self.unit_id):
            if value is not None and not value.strip():
                raise ValueError("Lesson title, course and unit must not be blank")
        return self

    def _snapshot(self):
        if self.book_id not in self.authorized_book_ids:
            raise BookAccessDenied("Book is outside the authorized scope")
        active = self.repository.active_index(self.book_id)
        index = json.loads(self.storage.get(active["object_key"], active["sha256"]))
        pipeline = json.loads(active["pipeline_json"])
        expected = {
            "book_id": self.book_id,
            "book_version": active["book_version"],
            "sha256": active["source_sha256"],
            "job_id": active["job_id"],
            "pipeline_fingerprint": active["pipeline_fingerprint"],
        }
        if type(index.get("book_version")) is not int or any(
            index.get(key) != value for key, value in expected.items()
        ):
            raise InputRejected("Active index identity does not match the authorized book/version")
        if (
            index.get("pipeline") != pipeline
            or type(index.get("schema_version")) is not int
            or index.get("schema_version") not in (1, 2)
        ):
            raise InputRejected("Stored index configuration or schema changed")
        docs, vectors = index.get("documents", []), index.get("vectors", [])
        if not docs or len(docs) != len(vectors):
            raise InputRejected("Invalid active index document/vector count")
        if len({doc.get("id") for doc in docs}) != len(docs):
            raise InputRejected("Active index contains duplicate chunk IDs")
        for doc, vector in zip(docs, vectors, strict=True):
            meta = doc["metadata"]
            if (
                meta.get("book_id") != self.book_id
                or meta.get("book_version") != active["book_version"]
                or meta.get("sha256") != active["source_sha256"]
            ):
                raise InputRejected("Active index contains a foreign book or version")
            if index["schema_version"] == 1:
                # Preserve existing page indexes; re-ingest to obtain grammar-point grouping.
                meta.update(
                    version=meta["book_version"],
                    chunk_id=doc["id"],
                    source_pages=[meta.get("pdf_page_number", meta["pdf_page_index"])],
                    course_id=meta.get("course_id"),
                    unit_id=meta.get("unit_id"),
                    lesson_number=meta.get("lesson_number"),
                )
            if (
                type(meta.get("version")) is not int
                or meta.get("version") != active["book_version"]
                or meta.get("chunk_id") != doc["id"]
                or any(
                    key not in meta
                    for key in ("course_id", "unit_id", "lesson_number", "source_pages")
                )
                or not meta["source_pages"]
            ):
                raise InputRejected("Active chunk is missing validated identity metadata")
            if index["schema_version"] == 2 and (
                not isinstance(meta["source_pages"], list)
                or any(type(p) is not int or p < 1 for p in meta["source_pages"])
            ):
                raise InputRejected("Active chunk has invalid source pages")
            check_vector(vector, pipeline["embedding_dimensions"])
        return active, index

    def _still_active(self, active):
        if self.repository.active_index(self.book_id)["index_version"] != active["index_version"]:
            raise ActiveIndexChanged("Active index changed during retrieval; retry the request")

    def _in_context(self, doc):
        meta = doc["metadata"]
        return (self.course_id is None or meta.get("course_id") == self.course_id) and (
            self.unit_id is None or meta.get("unit_id") == self.unit_id
        )

    @staticmethod
    def _lesson_match(meta, number, title):
        return (number is None or meta.get("lesson_number") == number) and (
            title is None
            or normalized_title(meta.get("lesson_title") or "") == normalized_title(title)
        )

    @staticmethod
    def _document(doc, active, scope, score=None):
        return Document(
            id=doc["id"],
            page_content=doc["text"],
            metadata={
                **doc["metadata"],
                "index_version": active["index_version"],
                "retrieval_scope": scope,
                **({"score": score} if score is not None else {}),
            },
        )

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        if not query.strip():
            raise InputRejected("Retrieval query must not be empty")
        active, index = self._snapshot()
        candidates = [i for i, doc in enumerate(index["documents"]) if self._in_context(doc)]
        lesson_supplied = self.current_lesson is not None or self.current_lesson_title is not None
        lesson = {
            i
            for i in candidates
            if self._lesson_match(
                index["documents"][i]["metadata"], self.current_lesson, self.current_lesson_title
            )
        }
        if lesson_supplied and (not self.allow_book_fallback or self.max_fallback == 0):
            candidates = [i for i in candidates if i in lesson]
        if not candidates:
            self._still_active(active)
            return []
        vector = self.embedding_factory(index["pipeline"]).embed_query(query)
        check_vector(vector, index["pipeline"]["embedding_dimensions"])
        scores = {i: similarity(vector, index["vectors"][i]) for i in candidates}
        ranked = sorted(
            (i for i in candidates if scores[i] >= self.min_score), key=lambda i: (-scores[i], i)
        )
        if lesson_supplied:
            primary = [i for i in ranked if i in lesson][: self.top_k]
            fallback = (
                [i for i in ranked if i not in lesson][
                    : min(self.top_k - len(primary), self.max_fallback)
                ]
                if self.allow_book_fallback
                else []
            )
            selected = [(i, "current_lesson") for i in primary] + [
                (i, "same_book_fallback") for i in fallback
            ]
        else:
            selected = [(i, "book") for i in ranked[: self.top_k]]
        self._still_active(active)
        return [
            self._document(index["documents"][i], active, scope, scores[i]) for i, scope in selected
        ]

    def lookup_lesson(
        self, *, lesson_number: int | None = None, title: str | None = None
    ) -> list[Document]:
        """Exact metadata lookup, in source order, without embedding or fuzzy matching."""
        if lesson_number is None and title is None:
            raise InputRejected("Supply a lesson number or title")
        if lesson_number is not None and (type(lesson_number) is not int or lesson_number < 1):
            raise InputRejected("Lesson number must be a positive integer")
        if title is not None and not title.strip():
            raise InputRejected("Lesson title must not be empty")
        active, index = self._snapshot()
        matched = [
            doc
            for doc in index["documents"]
            if self._in_context(doc) and self._lesson_match(doc["metadata"], lesson_number, title)
        ]
        self._still_active(active)
        return [self._document(doc, active, "exact_lesson") for doc in matched]

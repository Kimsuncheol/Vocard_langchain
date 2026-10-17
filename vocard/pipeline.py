import json

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from vocard.chunking import split_grammar_points
from vocard.extraction import extract_pages  # noqa: F401 - compatibility import
from vocard.ingestion import InputRejected
from vocard.retrieval import ActiveBookRetriever  # noqa: F401 - public compatibility import
from vocard.vectors import check_vector, embeddings_for, similarity  # noqa: F401


def split_pages(pages: list[Document], job: dict) -> list[Document]:
    return split_grammar_points(pages, job)


def build_index(chunks: list[Document], job: dict, embeddings: Embeddings) -> dict:
    pipeline = json.loads(job["pipeline_json"])
    vectors = []
    # Bounded requests: a large textbook never becomes a single provider request.
    for i in range(0, len(chunks), 32):
        batch = embeddings.embed_documents([d.page_content for d in chunks[i : i + 32]])
        if len(batch) != len(chunks[i : i + 32]):
            raise InputRejected("Embedding count does not match chunk count")
        for vector in batch:
            check_vector(vector, pipeline["embedding_dimensions"])
        vectors.extend(batch)
    return {
        "schema_version": 2,
        "job_id": job["job_id"],
        "book_id": job["book_id"],
        "book_version": job["book_version"],
        "sha256": job["sha256"],
        "pipeline_fingerprint": job["pipeline_fingerprint"],
        "pipeline": pipeline,
        "documents": [{"id": d.id, "text": d.page_content, "metadata": d.metadata} for d in chunks],
        "vectors": vectors,
    }


def validate_index(
    index: dict, expected_chunks: list[Document], job: dict, embeddings: Embeddings
) -> dict:
    pipeline = json.loads(job["pipeline_json"])
    for field in ("job_id", "book_id", "book_version", "sha256", "pipeline_fingerprint"):
        if index.get(field) != job[field]:
            raise InputRejected(f"Index {field} does not match the ingestion job")
    if index.get("schema_version") != 2 or index.get("pipeline") != pipeline:
        raise InputRejected("Index schema or embedding configuration mismatch")
    docs, vectors = index.get("documents", []), index.get("vectors", [])
    expected = [
        {"id": d.id, "text": d.page_content, "metadata": d.metadata} for d in expected_chunks
    ]
    if docs != expected or not docs or len(vectors) != len(docs):
        raise InputRejected("Stored index dropped or changed documents or vectors")
    if len({d["id"] for d in docs}) != len(docs):
        raise InputRejected("Duplicate chunk IDs")
    for doc in docs:
        meta = doc["metadata"]
        if (
            meta.get("book_id") != job["book_id"]
            or meta.get("version") != job["book_version"]
            or meta.get("chunk_id") != doc["id"]
            or any(
                key not in meta for key in ("course_id", "unit_id", "lesson_number", "source_pages")
            )
            or not meta["source_pages"]
        ):
            raise InputRejected("Invalid chunk identity or lesson metadata")
    for vector in vectors:
        check_vector(vector, pipeline["embedding_dimensions"])
    query_vector = embeddings.embed_query(docs[0]["text"])
    check_vector(query_vector, pipeline["embedding_dimensions"])
    smoke_score = similarity(query_vector, vectors[0])
    if smoke_score < 0.95:
        raise InputRejected("Stored index failed the embedding/query consistency smoke test")
    return {
        "passed": True,
        "chunk_count": len(docs),
        "page_count_indexed": len({p for d in docs for p in d["metadata"]["source_pages"]}),
        "dimensions": pipeline["embedding_dimensions"],
        "query_smoke_score": smoke_score,
        "uncertain_chunks": sum(bool(d["metadata"]["uncertain"]) for d in docs),
        "checks": [
            "source_sha256",
            "roundtrip_sha256",
            "complete_source_text_span_coverage",
            "exact_document_and_metadata_match",
            "chunk_count",
            "unique_chunk_ids",
            "chunk_identity_and_lesson_metadata",
            "finite_nonzero_vectors",
            "embedding_dimensions",
            "query_embedding_consistency",
        ],
    }

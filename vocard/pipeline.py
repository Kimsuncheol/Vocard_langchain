import hashlib
import json
import math

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from vocard.extraction import extract_pages  # noqa: F401 - compatibility import
from vocard.ingestion import InputRejected


def embeddings_for(pipeline: dict) -> Embeddings:
    if pipeline["embedding_provider"] != "openai":
        raise InputRejected("Unsupported embedding provider")
    return OpenAIEmbeddings(
        model=pipeline["embedding_model"],
        dimensions=pipeline["embedding_dimensions"],
        max_retries=2,
        request_timeout=60,
    )


def split_pages(pages: list[Document], job: dict) -> list[Document]:
    pipeline = json.loads(job["pipeline_json"])
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=pipeline["chunk_size"],
        chunk_overlap=pipeline["chunk_overlap"],
        separators=["\n\n", "\n", ". ", " ", ""],
        keep_separator=True,
        strip_whitespace=False,
        add_start_index=True,
    )
    chunks = splitter.split_documents(pages)
    if not 0 < len(chunks) <= pipeline["max_chunks"]:
        raise InputRejected("Chunk count is zero or exceeds the configured limit")
    source_pages = {d.metadata["pdf_page_index"]: d.page_content for d in pages}
    intervals = {page_index: [] for page_index in source_pages}
    for chunk in chunks:
        page_index = chunk.metadata["pdf_page_index"]
        start = chunk.metadata["start_index"]
        end = start + len(chunk.page_content)
        if start < 0 or source_pages[page_index][start:end] != chunk.page_content:
            raise InputRejected("Chunk text is not an exact source page span")
        intervals[page_index].append((start, end))
    for page_index, spans in intervals.items():
        covered = 0
        for start, end in sorted(spans):
            if start > covered:
                raise InputRejected("Chunk splitting dropped source text")
            covered = max(covered, end)
        if covered != len(source_pages[page_index]):
            raise InputRejected("Chunk splitting dropped a source page or its trailing text")
    for i, chunk in enumerate(chunks):
        identity = f"{job['job_id']}:{i}:{chunk.page_content}"
        chunk.id = hashlib.sha256(identity.encode()).hexdigest()
    return chunks


def check_vector(vector, dimensions):
    if (
        not isinstance(vector, list)
        or len(vector) != dimensions
        or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)
        or not any(v != 0 for v in vector)
    ):
        raise InputRejected("Embedding must be nonzero, finite and match configured dimensions")


def similarity(a, b):
    return math.fsum(x * y for x, y in zip(a, b, strict=True)) / (
        math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(x * x for x in b))
    )


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
        "schema_version": 1,
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
    if index.get("schema_version") != 1 or index.get("pipeline") != pipeline:
        raise InputRejected("Index schema or embedding configuration mismatch")
    docs, vectors = index.get("documents", []), index.get("vectors", [])
    expected = [
        {"id": d.id, "text": d.page_content, "metadata": d.metadata} for d in expected_chunks
    ]
    if docs != expected or not docs or len(vectors) != len(docs):
        raise InputRejected("Stored index dropped or changed documents or vectors")
    if len({d["id"] for d in docs}) != len(docs):
        raise InputRejected("Duplicate chunk IDs")
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
        "page_count_indexed": len({d["metadata"]["pdf_page_index"] for d in docs}),
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
            "finite_nonzero_vectors",
            "embedding_dimensions",
            "query_embedding_consistency",
        ],
    }


class ActiveBookRetriever(BaseRetriever):
    """Resolve the active pointer per invocation; in-flight queries use one snapshot."""

    repository: object
    storage: object
    embedding_factory: object = embeddings_for
    book_id: str
    k: int = 4

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        active = self.repository.active_index(self.book_id)
        index = json.loads(self.storage.get(active["object_key"], active["sha256"]))
        if index["book_id"] != self.book_id:
            raise InputRejected("Stored index belongs to another book")
        pipeline = json.loads(active["pipeline_json"])
        if index["pipeline"] != pipeline:
            raise InputRejected("Stored index embedding configuration changed")
        embeddings = self.embedding_factory(pipeline)
        query_vector = embeddings.embed_query(query)
        check_vector(query_vector, pipeline["embedding_dimensions"])
        scores = [similarity(query_vector, vector) for vector in index["vectors"]]
        best = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[: self.k]
        return [
            Document(
                id=index["documents"][i]["id"],
                page_content=index["documents"][i]["text"],
                metadata={
                    **index["documents"][i]["metadata"],
                    "index_version": active["index_version"],
                    "score": scores[i],
                },
            )
            for i in best
        ]

import hashlib
import io
import json
import unicodedata

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from pydantic import ValidationError
from pypdf import PdfWriter

from vocard.chunking import split_grammar_points, validate_source_coverage
from vocard.ingestion import InputRejected
from vocard.retrieval import ActiveBookRetriever, ActiveIndexChanged, BookAccessDenied
from vocard.worker import Worker

from .conftest import token


class SemanticEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        return ([1.0, 0.1] if "동명사" in text or "swimming" in text else [0.1, 1.0]) + [0.0] * 6


@pytest.fixture
def curriculum(system):
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=612, height=792)
    pdf = io.BytesIO()
    writer.write(pdf)
    pdf = pdf.getvalue()
    pages = [
        {
            "pdf_page_index": 1,
            "lesson_number": 1,
            "lesson_title": "동명사",
            "unit_id": "u1",
            "text": (
                "■ 동명사의 목적어\n동명사는 목적어로 사용된다.\n"
                "○ I enjoyed swimming.\n난 수영을 즐겼다."
            ),
            "headings": [{"text": "■ 동명사의 목적어"}],
            "explanations": [{"text": "동명사는 목적어로 사용된다."}],
            "examples": [
                {
                    "text": "○ I enjoyed swimming.",
                    "is_correct": True,
                    "translations": ["난 수영을 즐겼다."],
                }
            ],
        },
        {
            "pdf_page_index": 2,
            "lesson_number": 1,
            "lesson_title": "동명사",
            "unit_id": "u1",
            "text": "이어서 동명사를 설명한다.\n× I enjoyed to swim.\n수영을 즐겼다.",
            "examples": [
                {
                    "text": "× I enjoyed to swim.",
                    "is_correct": False,
                    "translations": ["수영을 즐겼다."],
                }
            ],
        },
        {
            "pdf_page_index": 3,
            "lesson_number": 2,
            "lesson_title": "가정법 과거완료",
            "unit_id": "u1",
            "text": (
                "■ 가정법의 형태\n과거의 가정을 표현한다.\n"
                "○ If I had known, I would have called.\n알았다면 전화했을 것이다.\n"
                "■ 가정법의 쓰임\n실제로 그렇지 않았던 일을 설명한다."
            ),
            "headings": [{"text": "■ 가정법의 형태"}, {"text": "■ 가정법의 쓰임"}],
            "examples": [
                {
                    "text": "○ If I had known, I would have called.",
                    "is_correct": True,
                    "translations": ["알았다면 전화했을 것이다."],
                }
            ],
        },
        {
            "pdf_page_index": 4,
            "lesson_number": 1,
            "lesson_title": "동명사",
            "unit_id": "u2",
            "text": "■ 다른 단원의 동명사\n별도 단원의 동명사 설명과 예문이다.",
            "headings": [{"text": "■ 다른 단원의 동명사"}],
        },
    ]

    def ingest(book_id="curriculum", source=pdf):
        payload = json.dumps(
            {
                "source": {"sha256": hashlib.sha256(source).hexdigest(), "page_index_base": 1},
                "pages": pages,
            },
            ensure_ascii=False,
        ).encode()
        job = system["service"].submit(
            book_id,
            io.BytesIO(source),
            "admin",
            io.BytesIO(payload),
            reviewed_extraction=True,
            course_id="c1",
            chunk_size=500,
        )
        Worker(
            system["settings"], system["repo"], system["storage"], lambda _: SemanticEmbeddings()
        ).run_once()
        assert system["repo"].job(job["job_id"])["status"] == "succeeded"
        return job

    job = ingest()
    return {**system, "job": job, "ingest": ingest, "pdf": pdf, "pages": pages}


def retriever(system, **overrides):
    return ActiveBookRetriever(
        repository=system["repo"],
        storage=system["storage"],
        embedding_factory=lambda _: SemanticEmbeddings(),
        book_id="curriculum",
        authorized_book_ids=frozenset({"curriculum"}),
        **overrides,
    )


def test_key_point_keeps_cross_page_explanation_and_examples_together(curriculum):
    docs = retriever(curriculum, unit_id="u1").lookup_lesson(lesson_number=1)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.metadata["source_pages"] == [1, 2]
    assert "동명사는 목적어로 사용된다." in doc.page_content
    assert "I enjoyed swimming." in doc.page_content and "I enjoyed to swim." in doc.page_content
    assert "난 수영을 즐겼다." in doc.page_content
    assert doc.metadata["grammar"]["examples"][1]["is_correct"] is False
    for key in (
        "book_id",
        "version",
        "course_id",
        "unit_id",
        "lesson_number",
        "source_pages",
        "chunk_id",
    ):
        assert key in doc.metadata
    assert doc.id == doc.metadata["chunk_id"] and doc.metadata["version"] == 1
    assert doc.metadata["course_id"] == "c1" and doc.metadata["unit_id"] == "u1"


def test_lesson_first_with_bounded_same_book_fallback(curriculum):
    docs = retriever(curriculum, current_lesson=2, top_k=4, max_fallback=1).invoke("swimming")
    assert [d.metadata["lesson_number"] for d in docs] == [2, 2, 1]
    assert [d.metadata["retrieval_scope"] for d in docs] == [
        "current_lesson",
        "current_lesson",
        "same_book_fallback",
    ]
    assert (
        docs[-1].metadata["score"] > docs[0].metadata["score"]
    )  # Preference outranks global similarity.
    assert all(d.metadata["book_id"] == "curriculum" and d.metadata["version"] == 1 for d in docs)
    strict = retriever(curriculum, current_lesson=2, top_k=4, allow_book_fallback=False).invoke(
        "swimming"
    )
    assert len(strict) == 2 and all(d.metadata["lesson_number"] == 2 for d in strict)
    assert (
        retriever(curriculum, current_lesson=99, allow_book_fallback=False).invoke("swimming") == []
    )
    assert len(retriever(curriculum, current_lesson=99, max_fallback=1).invoke("swimming")) == 1


def test_score_threshold_and_top_k_are_enforced(curriculum):
    assert len(retriever(curriculum, top_k=1).invoke("swimming")) == 1
    assert (
        retriever(curriculum, current_lesson=2, min_score=0.9, allow_book_fallback=False).invoke(
            "swimming"
        )
        == []
    )
    fallback = retriever(curriculum, current_lesson=2, min_score=0.9, max_fallback=1).invoke(
        "swimming"
    )
    assert len(fallback) == 1 and fallback[0].metadata["retrieval_scope"] == "same_book_fallback"


def test_course_and_unit_narrow_both_primary_and_fallback(curriculum):
    docs = retriever(curriculum, current_lesson=2, course_id="c1", unit_id="u1").invoke("swimming")
    assert all(d.metadata["unit_id"] == "u1" for d in docs)
    assert retriever(curriculum, course_id="not-this-course").invoke("swimming") == []


def test_exact_lookup_has_no_embedding_fallback_or_partial_title_match(curriculum):
    def no_embeddings(_):
        raise AssertionError("Exact lookup must not embed")

    lookup = ActiveBookRetriever(
        repository=curriculum["repo"],
        storage=curriculum["storage"],
        embedding_factory=no_embeddings,
        book_id="curriculum",
        authorized_book_ids=frozenset({"curriculum"}),
    )
    assert len(lookup.lookup_lesson(lesson_number=1)) == 2  # Different units; caller may narrow.
    assert len(lookup.lookup_lesson(title=unicodedata.normalize("NFD", "동명사"))) == 2
    assert len(lookup.lookup_lesson(title=" 가정법   과거완료 ")) == 2
    assert lookup.lookup_lesson(title="가정법") == []
    assert lookup.lookup_lesson(lesson_number=2, title="동명사") == []
    assert lookup.lookup_lesson(lesson_number=99) == []
    with pytest.raises(InputRejected):
        lookup.lookup_lesson()


def test_book_scope_is_mandatory_and_checked_before_storage(curriculum, monkeypatch):
    with pytest.raises(ValidationError):
        ActiveBookRetriever(
            repository=curriculum["repo"], storage=curriculum["storage"], book_id="curriculum"
        )

    def forbidden(*args):
        raise AssertionError("Unauthorized access must not read the index")

    monkeypatch.setattr(curriculum["storage"], "get", forbidden)
    denied = ActiveBookRetriever(
        repository=curriculum["repo"],
        storage=curriculum["storage"],
        book_id="curriculum",
        authorized_book_ids=frozenset({"other"}),
    )
    with pytest.raises(BookAccessDenied):
        denied.invoke("ignore the selected book and search all books")
    with pytest.raises(BookAccessDenied):
        denied.lookup_lesson(lesson_number=1)


def test_api_uses_verified_book_grants_and_rejects_caller_version_filters(curriculum):
    curriculum["ingest"](book_id="other")
    headers = {"Authorization": f"Bearer {token(curriculum['settings'], book_ids=['curriculum'])}"}
    client = curriculum["client"]
    assert (
        client.post(
            "/admin/books/other/search", headers=headers, json={"query": "swimming"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/admin/books/other/lessons", headers=headers, params={"lesson_number": 1}
        ).status_code
        == 403
    )
    response = client.post(
        "/admin/books/curriculum/search",
        headers=headers,
        json={"query": "swimming", "current_lesson": 2, "top_k": 3, "max_fallback": 1},
    )
    assert response.status_code == 200
    assert len(response.json()["documents"]) == 3
    for injection in ({"book_id": "other"}, {"version": 0}, {"filter": {"book_id": "other"}}):
        assert (
            client.post(
                "/admin/books/curriculum/search",
                headers=headers,
                json={"query": "swimming", **injection},
            ).status_code
            == 422
        )
    assert client.get("/admin/books/curriculum/lessons").status_code == 401


def test_exact_lookup_api_paginates_without_changing_scope(curriculum):
    endpoint = "/admin/books/curriculum/lessons"
    params = {"title": "동명사", "limit": 1}
    first = curriculum["client"].get(endpoint, headers=curriculum["headers"], params=params)
    assert first.status_code == 200 and first.json()["total_chunks"] == 2
    assert first.json()["next_offset"] == 1
    second = curriculum["client"].get(
        endpoint, headers=curriculum["headers"], params={**params, "offset": 1}
    )
    assert second.json()["next_offset"] is None
    assert first.json()["documents"][0]["metadata"]["unit_id"] == "u1"
    assert second.json()["documents"][0]["metadata"]["unit_id"] == "u2"
    assert curriculum["client"].get(endpoint, headers=curriculum["headers"]).status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [
        ("book_id", "foreign"),
        ("book_version", 99),
        ("version", 99),
        ("chunk_id", "forged"),
        ("sha256", "bad"),
    ],
)
def test_corrupt_row_identity_fails_closed_even_with_valid_object_checksum(
    curriculum, field, value
):
    active = curriculum["repo"].active_index("curriculum")
    index = json.loads(curriculum["storage"].get(active["object_key"], active["sha256"]))
    index["documents"][0]["metadata"][field] = value
    payload = json.dumps(index).encode()
    sha = hashlib.sha256(payload).hexdigest()
    curriculum["s3"].put_object(
        Bucket=curriculum["settings"].bucket,
        Key=active["object_key"],
        Body=payload,
        Metadata={"sha256": sha},
    )
    with curriculum["repo"].connection(write=True) as con:
        con.execute(
            "UPDATE index_versions SET sha256=? WHERE index_version=?",
            (sha, active["index_version"]),
        )
    with pytest.raises(InputRejected):
        retriever(curriculum).invoke("swimming")
    with pytest.raises(InputRejected):
        retriever(curriculum).lookup_lesson(lesson_number=1)


def test_retriever_and_exact_lookup_follow_active_version_on_every_call(curriculum):
    search = retriever(curriculum)
    previous = search.invoke("swimming")
    writer = PdfWriter()
    writer.append(io.BytesIO(curriculum["pdf"]))
    writer.add_metadata({"/Title": "version two"})
    output = io.BytesIO()
    writer.write(output)
    curriculum["ingest"](source=output.getvalue())
    current = search.invoke("swimming")
    assert all(d.metadata["version"] == 2 for d in current)
    assert all(d.metadata["version"] == 2 for d in search.lookup_lesson(lesson_number=1))
    assert {d.id for d in previous}.isdisjoint({d.id for d in current})


def test_mid_query_promotion_returns_no_stale_results(curriculum, monkeypatch):
    original = curriculum["repo"].active_index
    calls = []

    def changed(book_id):
        active = original(book_id)
        calls.append(True)
        return active if len(calls) == 1 else {**active, "index_version": "newly-published"}

    monkeypatch.setattr(curriculum["repo"], "active_index", changed)
    with pytest.raises(ActiveIndexChanged):
        retriever(curriculum).invoke("swimming")


def test_chunk_size_bounds_and_stable_ids_preserve_long_examples(system):
    job = {
        "book_id": "book",
        "book_version": 1,
        "sha256": "a" * 64,
        "pipeline_fingerprint": "stable",
        "job_id": "first",
        "pipeline_json": json.dumps({"chunk_size": 120, "chunk_overlap": 10, "max_chunks": 100}),
    }
    example = "○ I enjoyed swimming.\n난 수영을 즐겼다."
    text = ("설명 문장을 원문 그대로 보존한다. " * 15) + "\n" + example
    page = Document(
        page_content=text,
        metadata={
            "book_id": "book",
            "book_version": 1,
            "sha256": "a" * 64,
            "pdf_page_index": 1,
            "grammar": {"examples": [{"text": example}]},
        },
    )
    chunks = split_grammar_points([page], job)
    assert len(chunks) > 1 and max(len(d.page_content) for d in chunks) <= 120
    assert any(example in d.page_content for d in chunks)
    validate_source_coverage([page], chunks)
    assert [d.id for d in chunks] == [
        d.id for d in split_grammar_points([page], {**job, "job_id": "retry"})
    ]


def test_automatic_lesson_headers_and_ambiguous_ocr_do_not_mislabel_points(system):
    job = {
        "book_id": "book",
        "book_version": 1,
        "sha256": "a" * 64,
        "pipeline_fingerprint": "stable",
        "pipeline_json": json.dumps({"chunk_size": 500, "chunk_overlap": 10, "max_chunks": 100}),
    }
    pages = []
    for n, heading in enumerate(("Must 7\n동명사", "Must O8\n부정사", "Must 9\n가정법"), 1):
        pages.append(
            Document(
                page_content=heading + "\n원문 설명을 그대로 유지한다.",
                metadata={
                    "book_id": "book",
                    "book_version": 1,
                    "sha256": "a" * 64,
                    "pdf_page_index": n,
                    "grammar": {"headings": [{"text": heading}]},
                },
            )
        )
    chunks = split_grammar_points(pages, job)
    assert [c.metadata["lesson_number"] for c in chunks] == [7, None, 9]
    assert chunks[0].metadata["lesson_title"] == "동명사"
    assert chunks[1].metadata["lesson_title"] is None


def test_old_published_page_indexes_remain_readable_with_scope_checks(curriculum):
    active = curriculum["repo"].active_index("curriculum")
    index = json.loads(curriculum["storage"].get(active["object_key"], active["sha256"]))
    index["schema_version"] = 1
    for doc in index["documents"]:
        meta = doc["metadata"]
        for key in ("version", "chunk_id", "source_pages", "course_id", "unit_id", "lesson_number"):
            meta.pop(key)
        meta["pdf_page_index"] = meta["pdf_page_number"] - 1
    payload = json.dumps(index).encode()
    sha = hashlib.sha256(payload).hexdigest()
    curriculum["s3"].put_object(
        Bucket=curriculum["settings"].bucket,
        Key=active["object_key"],
        Body=payload,
        Metadata={"sha256": sha},
    )
    with curriculum["repo"].connection(write=True) as con:
        con.execute(
            "UPDATE index_versions SET sha256=? WHERE index_version=?",
            (sha, active["index_version"]),
        )
    docs = retriever(curriculum).invoke("swimming")
    assert docs and all(
        d.metadata["book_id"] == "curriculum" and d.metadata["version"] == 1 for d in docs
    )
    assert all(p >= 1 for d in docs for p in d.metadata["source_pages"])

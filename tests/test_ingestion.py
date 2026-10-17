import hashlib
import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from vocard.db import LeaseLost, Repository
from vocard.ingestion import IngestionService, InputRejected
from vocard.pipeline import ActiveBookRetriever, extract_pages, split_pages
from vocard.worker import Worker

from .conftest import TestEmbeddings, make_pdf, make_sidecar, token


def submit(system, pdf=None, **kwargs):
    return system["service"].submit(
        "grammar", io.BytesIO(pdf or make_pdf()), "test-admin", **kwargs
    )


def expire(repo, job_id):
    with repo.connection(write=True) as con:
        con.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (time.time() - 1, job_id))


def ready(repo, job_id):
    with repo.connection(write=True) as con:
        con.execute("UPDATE jobs SET available_at=0 WHERE job_id=?", (job_id,))


def test_api_admin_only_before_multipart_parsing(system):
    client, settings = system["client"], system["settings"]
    url = "/admin/books/grammar/ingestions"
    assert client.post(url, content=b"invalid multipart").status_code == 401
    user = {"Authorization": f"Bearer {token(settings, role='user')}"}
    assert client.post(url, headers=user).status_code == 403
    for overrides in ({"exp": 1}, {"aud": "other"}, {"iss": "other"}, {"sub": ""}):
        headers = {"Authorization": f"Bearer {token(settings, **overrides)}"}
        assert client.post(url, headers=headers).status_code == 401
    assert client.get("/admin/books/grammar").status_code == 401
    assert client.get("/admin/jobs/unknown").status_code == 401
    assert client.post("/admin/jobs/unknown/retry").status_code == 401
    assert client.post("/admin/books/grammar/search", json={"query": "test"}).status_code == 401
    assert system["s3"].list_objects_v2(Bucket=settings.bucket)["KeyCount"] == 0


def test_upload_is_idempotent_and_worker_is_separate(system):
    client, headers = system["client"], system["headers"]
    pdf = make_pdf()
    url = "/admin/books/grammar/ingestions"
    first = client.post(url, headers=headers, files={"file": ("book.pdf", pdf)})
    second = client.post(url, headers=headers, files={"file": ("again.pdf", pdf)})
    assert first.status_code == second.status_code == 202
    job = first.json()
    assert job["created"] is True and second.json()["created"] is False
    assert job["job_id"] == second.json()["job_id"]
    assert job["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert job["book_version"] == 1 and job["status"] == "queued"
    assert "lease_token" not in job
    assert system["repo"].book("grammar")["active_index_version"] is None
    assert (
        client.post(
            "/admin/books/grammar/search", headers=headers, json={"query": "Where am I?"}
        ).status_code
        == 404
    )
    # Reopen the persistent queue as a newly started worker would.
    worker = Worker(
        system["settings"],
        Repository(system["settings"].db_path),
        system["storage"],
        lambda _: TestEmbeddings(),
    )
    assert worker.run_once() is True and worker.run_once() is False
    status = client.get(f"/admin/jobs/{job['job_id']}", headers=headers).json()
    assert status["status"] == "succeeded" and status["extraction_status"] == "succeeded"
    result = client.post(
        "/admin/books/grammar/search", headers=headers, json={"query": "Where am I?"}
    )
    assert result.status_code == 200
    document = result.json()["documents"][0]
    assert "Where am I?" in document["page_content"]
    assert document["metadata"]["pdf_page_index"] == 1
    assert (
        document["metadata"]["index_version"]
        == system["repo"].book("grammar")["active_index_version"]
    )
    source = system["s3"].head_object(Bucket=system["settings"].bucket, Key=job["source_key"])
    assert source["ServerSideEncryption"] == "AES256"


def test_concurrent_enqueues_and_claims(system):
    repo, settings = system["repo"], system["settings"]

    def enqueue(_):
        return repo.enqueue(
            "grammar", "a" * 64, "source", None, settings.pipeline(None, False), "admin", 3
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(enqueue, range(12)))
    assert len({j["job_id"] for j in jobs}) == 1
    assert sum(j["created"] for j in jobs) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: repo.claim(120), range(12)))
    assert sum(j is not None for j in claimed) == 1


def test_new_version_replaces_only_after_success_and_keeps_history(system):
    first = submit(system)
    system["worker"].run_once()
    previous = system["repo"].active_index("grammar")
    next_job = submit(system, make_pdf("This is the second book version.", title="v2"))
    assert next_job["book_version"] == 2
    assert system["repo"].active_index("grammar") == previous
    system["worker"].run_once()
    book = system["repo"].book("grammar")
    assert book["active_index_version"] != previous["index_version"]
    assert len(book["indexes"]) == 2
    assert book["indexes"][1]["previous_index_version"] == previous["index_version"]
    assert system["storage"].get(previous["object_key"], previous["sha256"])
    assert system["repo"].job(first["job_id"])["status"] == "succeeded"


@pytest.mark.parametrize("bad", [[], [0.0] * 8, [math.nan] * 8, [1.0] * 7])
def test_invalid_vectors_never_publish_or_replace_active(system, bad):
    submit(system)
    system["worker"].run_once()
    old = system["repo"].active_index("grammar")
    job = submit(system, make_pdf(title="bad"))

    class Broken(TestEmbeddings):
        def embed_documents(self, texts):
            return [bad for _ in texts]

    Worker(system["settings"], system["repo"], system["storage"], lambda _: Broken()).run_once()
    assert system["repo"].job(job["job_id"])["status"] == "failed"
    assert system["repo"].active_index("grammar") == old
    assert len(system["repo"].book("grammar")["indexes"]) == 1


def test_query_validation_failure_preserves_active(system):
    submit(system)
    system["worker"].run_once()
    old = system["repo"].active_index("grammar")
    job = submit(system, make_pdf(title="wrong-query"))

    class WrongQuery(TestEmbeddings):
        def embed_documents(self, texts):
            return [TestEmbeddings().embed_query(t) for t in texts]

        def embed_query(self, text):
            return [-n for n in TestEmbeddings().embed_query(text)]

    Worker(system["settings"], system["repo"], system["storage"], lambda _: WrongQuery()).run_once()
    assert system["repo"].job(job["job_id"])["stage"] == "validating"
    assert system["repo"].job(job["job_id"])["status"] == "failed"
    assert system["repo"].active_index("grammar") == old


def test_expired_worker_is_reclaimed_and_fenced(system):
    job = submit(system)
    repo = system["repo"]
    stale = repo.claim(120)
    expire(repo, job["job_id"])
    assert system["worker"].run_once()
    assert repo.job(job["job_id"])["attempts"] == 2
    assert repo.job(job["job_id"])["status"] == "succeeded"
    active = repo.active_index("grammar")
    with pytest.raises(LeaseLost):
        repo.heartbeat(job["job_id"], stale["lease_token"], 120)
    with pytest.raises(LeaseLost):
        repo.publish(job["job_id"], stale["lease_token"], "stale-key", "sha", {"passed": True})
    assert repo.active_index("grammar") == active


def test_publish_requires_validation_stage_and_passing_report(system):
    submit(system)
    job = system["repo"].claim(120)
    with pytest.raises(ValueError, match="successful validation"):
        system["repo"].publish(job["job_id"], job["lease_token"], "key", "sha", {"passed": False})
    with pytest.raises(ValueError, match="not ready"):
        system["repo"].publish(job["job_id"], job["lease_token"], "key", "sha", {"passed": True})
    assert system["repo"].book("grammar")["active_index_version"] is None


def test_old_job_cannot_roll_back_a_newer_published_index(system):
    repo = system["repo"]
    submit(system)
    old = repo.claim(120)
    submit(system, make_pdf(title="new"))
    system["worker"].run_once()
    active = repo.active_index("grammar")
    # Repository-level interleaving test for a delayed but validated old worker.
    repo.stage(old["job_id"], old["lease_token"], "validating", "succeeded")
    repo.publish(old["job_id"], old["lease_token"], "old-immutable-key", "sha", {"passed": True})
    assert repo.job(old["job_id"])["publication_result"] == "superseded"
    assert repo.active_index("grammar") == active


def test_transient_failure_retries_and_admin_can_retry_exhausted_job(system):
    job = submit(system)

    class Unavailable(TestEmbeddings):
        def embed_documents(self, texts):
            raise ConnectionError("sensitive provider response should not be exposed")

    worker = Worker(system["settings"], system["repo"], system["storage"], lambda _: Unavailable())
    for attempt in range(1, 4):
        assert worker.run_once()
        status = system["repo"].job(job["job_id"])
        assert status["attempts"] == attempt
        assert status["last_error"] == "ConnectionError"
        assert status["status"] == ("queued" if attempt < 3 else "failed")
        ready(system["repo"], job["job_id"])
    response = system["client"].post(
        f"/admin/jobs/{job['job_id']}/retry", headers=system["headers"]
    )
    assert response.status_code == 202
    assert system["worker"].run_once()
    assert system["repo"].job(job["job_id"])["status"] == "succeeded"
    assert system["repo"].job(job["job_id"])["last_retried_by"] == "test-admin"


def test_worker_crash_at_retry_limit_becomes_failed(system):
    job = submit(system)
    repo = system["repo"]
    for _ in range(3):
        assert repo.claim(120)
        expire(repo, job["job_id"])
    assert repo.claim(120) is None
    assert repo.job(job["job_id"])["status"] == "failed"
    assert repo.book("grammar")["active_index_version"] is None


def test_pipeline_configuration_and_extraction_digest_are_in_idempotency_key(system):
    pdf = make_pdf()
    first = submit(system, pdf)
    settings = system["replace_settings"](pipeline_revision="4")
    service = IngestionService(settings, system["repo"], system["storage"])
    second = service.submit("grammar", io.BytesIO(pdf), "admin")
    third = submit(system, pdf, extraction=io.BytesIO(make_sidecar(pdf)), allow_uncertain=True)
    assert len({j["job_id"] for j in (first, second, third)}) == 3
    assert {j["book_version"] for j in (first, second, third)} == {1}


def test_scanned_pdf_requires_ocr_and_reviewed_import(system):
    pdf = make_pdf(text=None)
    native = submit(system, pdf)
    system["worker"].run_once()
    assert system["repo"].job(native["job_id"])["status"] == "failed"
    sidecar = make_sidecar(pdf)
    strict = submit(system, pdf, extraction=io.BytesIO(sidecar))
    system["worker"].run_once()
    assert system["repo"].job(strict["job_id"])["status"] == "failed"
    accepted = submit(system, pdf, extraction=io.BytesIO(sidecar), reviewed_extraction=True)
    system["worker"].run_once()
    assert system["repo"].job(accepted["job_id"])["status"] == "succeeded"
    retriever = ActiveBookRetriever(
        repository=system["repo"],
        storage=system["storage"],
        embedding_factory=lambda _: TestEmbeddings(),
        book_id="grammar",
        authorized_book_ids=frozenset({"grammar"}),
    )
    docs = retriever.invoke("가정법 과거완료")
    text = "".join(d.page_content for d in docs)
    assert "○ I enjoyed swimming." in text and "× I enjoyed to swim." in text
    assert all(d.metadata["uncertain"] for d in docs)


@pytest.mark.parametrize("mutation", ["hash", "missing-page", "wrong-index", "wrong-text"])
def test_extraction_must_match_source_and_page_coverage(system, mutation):
    pdf = make_pdf(text=None)
    sidecar = json.loads(make_sidecar(pdf))
    if mutation == "hash":
        sidecar["source"]["sha256"] = "0" * 64
    elif mutation == "missing-page":
        sidecar["pages"] = []
    elif mutation == "wrong-index":
        sidecar["pages"][0]["pdf_page_index"] = 1
    else:
        sidecar["pages"][0]["text"] = 123
    job = submit(
        system, pdf, extraction=io.BytesIO(json.dumps(sidecar).encode()), allow_uncertain=True
    )
    system["worker"].run_once()
    assert system["repo"].job(job["job_id"])["status"] == "failed"
    assert system["repo"].book("grammar")["active_index_version"] is None


def test_storage_fails_closed_and_rejects_corruption(system):
    job = submit(system)
    s3, bucket = system["s3"], system["settings"].bucket
    s3.put_object(
        Bucket=bucket, Key=job["source_key"], Body=b"corrupt", Metadata={"sha256": job["sha256"]}
    )
    with pytest.raises(ValueError, match="checksum"):
        system["storage"].get(job["source_key"], job["sha256"])
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    with pytest.raises(ValueError, match="Block Public Access"):
        system["storage"].put("test", io.BytesIO(b"data"), "sha", "application/pdf")


def test_upload_limits_and_pdf_signature(system):
    with pytest.raises(InputRejected, match="must be a PDF"):
        submit(system, b"not a pdf")
    settings = system["replace_settings"](max_upload_bytes=10)
    service = IngestionService(settings, system["repo"], system["storage"])
    with pytest.raises(InputRejected, match="byte limit"):
        service.submit("book", io.BytesIO(make_pdf()), "admin")
    response = system["client"].post(
        "/admin/books/grammar/ingestions",
        headers={
            **system["headers"],
            "Content-Length": "999999999999",
        },
        content=b"small",
    )
    assert response.status_code == 413


def test_heartbeat_keeps_slow_embedding_job_owned(system):
    settings = system["replace_settings"](lease_seconds=3)
    job = submit(system)

    class Slow(TestEmbeddings):
        def embed_documents(self, texts):
            time.sleep(3.5)
            return super().embed_documents(texts)

    worker = Worker(settings, system["repo"], system["storage"], lambda _: Slow())
    worker.run_once()
    assert system["repo"].job(job["job_id"])["status"] == "succeeded"
    assert system["repo"].job(job["job_id"])["attempts"] == 1


def test_no_text_is_never_published_even_with_allow_uncertain(system):
    job = submit(system, make_pdf(text=None), allow_uncertain=True)
    system["worker"].run_once()
    assert system["repo"].job(job["job_id"])["status"] == "failed"


def test_source_bytes_checked_again_during_extraction(system):
    job = submit(system)
    with pytest.raises(InputRejected, match="SHA-256"):
        extract_pages(make_pdf(title="different"), job, None)


def test_chunking_preserves_every_original_character_and_page_index(system):
    pdf = make_pdf(text=None)
    text = (
        '  가정법 과거완료: "If I had known, I would have called."\n'
        "○ I enjoyed swimming.  × I enjoyed to swim.\n\n"
    ) * 10 + "  \n"
    job = submit(
        system, pdf, extraction=io.BytesIO(make_sidecar(pdf, text)), reviewed_extraction=True
    )
    pages = extract_pages(pdf, job, make_sidecar(pdf, text))
    chunks = split_pages(pages, job)
    assert len(chunks) > 1
    intervals = []
    for chunk in chunks:
        start = chunk.metadata["start_index"]
        assert text[start : start + len(chunk.page_content)] == chunk.page_content
        assert chunk.metadata["pdf_page_index"] == 1
        intervals.append((start, start + len(chunk.page_content)))
    assert min(a for a, _ in intervals) == 0
    assert max(b for _, b in intervals) == len(text)


def test_corrupted_stored_build_cannot_replace_active_index(system, monkeypatch):
    submit(system)
    system["worker"].run_once()
    active = system["repo"].active_index("grammar")
    job = submit(system, make_pdf(title="roundtrip-corruption"))
    original = system["storage"].put

    def corrupt(key, body, sha, content_type):
        original(key, body, sha, content_type)
        if key.startswith("indexes/"):
            system["s3"].put_object(
                Bucket=system["settings"].bucket,
                Key=key,
                Body=b'{"documents": []}',
                Metadata={"sha256": sha},
            )

    monkeypatch.setattr(system["storage"], "put", corrupt)
    system["worker"].run_once()
    assert system["repo"].job(job["job_id"])["status"] == "queued"
    assert system["repo"].active_index("grammar") == active

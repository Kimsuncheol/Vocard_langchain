import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from firebase_admin import auth
from langchain_core.runnables import RunnableLambda

from vocard.api import create_app
from vocard.student_auth import FirebaseTokenVerifier
from vocard.usage import UsageCounters

from .test_answering import draft
from .test_retrieval import SemanticEmbeddings
from .test_retrieval import curriculum as curriculum_fixture

curriculum = curriculum_fixture
QUESTION = {
    "question": "동명사",
    "course_id": "c1",
    "unit_id": "u1",
    "lesson_number": 1,
}


def client_for(system, generate=draft, **overrides):
    settings = replace(system["settings"], course_books={"c1": "curriculum"}, **overrides)

    def verifier(token):
        if token == "expired":
            raise auth.InvalidIdTokenError("secret-token")
        if token == "anonymous":
            return {"uid": "guest", "firebase": {"sign_in_provider": "anonymous"}}
        return {"uid": token}

    return TestClient(
        create_app(
            settings,
            system["repo"],
            system["storage"],
            lambda _: SemanticEmbeddings(),
            lambda _: RunnableLambda(generate),
            verifier,
        )
    )


def ask(client, uid="alice", **changes):
    return client.post(
        "/ai/answer", json={**QUESTION, **changes}, headers={"Authorization": "Bearer " + uid}
    )


def test_independent_requests_and_active_version(curriculum):
    captured = []

    def generate(messages):
        captured.append(json.loads(messages[-1].content))
        return draft(messages)

    client = client_for(curriculum, generate)
    first = ask(client).json()
    curriculum["ingest"](source=curriculum["pdf"] + b"\n")
    second = ask(client, question="동명사의 목적어").json()
    assert first["citations"][0]["index_version"] != second["citations"][0]["index_version"]
    assert all("history" not in envelope for envelope in captured)
    assert captured[1]["question"] == "동명사의 목적어"
    assert "conversation_id" not in first
    assert client.get("/ai/conversations").status_code == 404
    assert client.get("/ai/conversations/anything").status_code == 404


def test_auth_scope_and_rejected_history(curriculum):
    client = client_for(curriculum)
    assert client.post("/ai/answer", json=QUESTION).status_code == 401
    assert ask(client, uid="expired").status_code == 401
    assert ask(client, uid="anonymous").status_code == 403
    assert ask(client, course_id="foreign").status_code == 403
    for extra in (
        {"conversation_id": None},
        {"previous_messages": []},
        {"messages": []},
        {"history": []},
        {"uid": "victim"},
        {"book_id": "foreign"},
    ):
        result = client.post(
            "/ai/answer", json={**QUESTION, **extra}, headers={"Authorization": "Bearer alice"}
        )
        assert result.status_code == 422
        assert "input" not in result.json()
        assert result.headers["cache-control"] == "no-store"


def test_context_optional_and_missing_reference_clarifies(curriculum):
    client = client_for(curriculum)
    result = client.post(
        "/ai/answer", json={"question": "동명사"}, headers={"Authorization": "Bearer alice"}
    )
    assert result.status_code == 200 and result.json()["status"] == "answered"
    result = ask(client, question="왜 그런가요?")
    assert result.json()["status"] == "clarification_needed"
    assert result.json()["citations"] == []


def test_quota_survives_restart_and_is_per_verified_uid(curriculum):
    client = client_for(curriculum, ai_daily_quota=1)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 429
    assert ask(client, uid="bob").status_code == 200
    assert ask(client_for(curriculum, ai_daily_quota=1)).status_code == 429


def test_quota_atomic(curriculum):
    store = UsageCounters(curriculum["repo"], replace(curriculum["settings"], ai_daily_quota=1))

    def reserve(_):
        try:
            store.reserve("alice")
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(reserve, range(4))) == [200, 429, 429, 429]


def test_no_content_in_database_or_telemetry(curriculum, monkeypatch, caplog, capsys):
    from langchain_core.caches import InMemoryCache
    from langchain_core.globals import set_debug, set_llm_cache, set_verbose
    from langchain_core.tracers.context import collect_runs

    canary = "PRIVATE_QUESTION_CANARY_042"
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    set_debug(True)
    set_verbose(True)
    cache = InMemoryCache()
    set_llm_cache(cache)
    try:
        client = client_for(curriculum)
        with collect_runs() as collector:
            result = ask(client, question="동명사 " + canary)
        assert result.status_code == 200, result.text
        assert collector.traced_runs == []
        assert cache._cache == {}
        captured_output = capsys.readouterr()
        assert canary not in caplog.text + captured_output.out + captured_output.err
        assert (
            result.json()["answer"] not in caplog.text + captured_output.out + captured_output.err
        )
        with curriculum["repo"].connection() as con:
            assert con.execute("SELECT count FROM ai_daily_usage").fetchone()["count"] == 1
            assert not con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='ai_conversations'"
            ).fetchone()
            dump = "\n".join(con.iterdump())
        assert canary not in dump and result.json()["answer"] not in dump
    finally:
        set_llm_cache(None)
        set_debug(False)
        set_verbose(False)


def test_generation_failure_and_validation_do_not_echo_content(curriculum):
    def broken(_):
        raise RuntimeError("PRIVATE_PROVIDER_BODY")

    client = client_for(curriculum, broken)
    result = ask(client, question="PRIVATE_QUESTION")
    assert result.status_code == 503
    assert "PRIVATE" not in result.text
    assert result.headers["cache-control"] == "no-store"
    result = ask(client, question={"secret": "PRIVATE_REJECTED_CONTENT"})
    assert result.status_code == 422 and "PRIVATE" not in result.text
    assert ask(client_for(curriculum)).status_code == 200


def test_legacy_data_blocks_start_without_deleting_it(curriculum):
    with curriculum["repo"].connection(write=True) as con:
        con.execute("CREATE TABLE ai_conversations (turns_json TEXT)")
        con.execute("INSERT INTO ai_conversations VALUES ('[]')")
    with pytest.raises(RuntimeError, match="Legacy conversation data exists"):
        client_for(curriculum)
    with curriculum["repo"].connection() as con:
        assert con.execute("SELECT COUNT(*) FROM ai_conversations").fetchone()[0] == 1


def test_real_verifier_requests_revocation_check(monkeypatch):
    captured = {}
    app = object()
    monkeypatch.setattr("firebase_admin.get_app", lambda _: app)

    def verify(token, **kwargs):
        captured.update(token=token, **kwargs)
        return {"uid": "verified-user"}

    monkeypatch.setattr(auth, "verify_id_token", verify)
    assert FirebaseTokenVerifier("vocard-6f1a5")("id-token")["uid"] == "verified-user"
    assert captured == {"token": "id-token", "app": app, "check_revoked": True}


def test_ambiguous_course_clarifies_without_guessing_book(curriculum):
    settings = replace(curriculum["settings"], course_books={"c1": "curriculum", "c2": "other"})
    client = TestClient(
        create_app(
            settings,
            curriculum["repo"],
            curriculum["storage"],
            lambda _: pytest.fail("Must select a book first"),
            firebase_token_verifier=lambda _: {"uid": "alice"},
        )
    )
    response = client.post(
        "/ai/answer", json={"question": "동명사?"}, headers={"Authorization": "Bearer token"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "clarification_needed"
    assert response.json()["citations"] == []


def test_legacy_migration_requires_explicit_opt_in_and_preserves_books_usage(curriculum):
    from vocard.privacy_migration import remove_legacy_conversations
    from vocard.usage import UsageCounters

    store = UsageCounters(curriculum["repo"], curriculum["settings"])
    store.reserve("alice")
    path = curriculum["settings"].db_path
    with curriculum["repo"].connection(write=True) as con:
        con.execute("CREATE TABLE ai_conversations (turns_json TEXT)")
        con.execute("INSERT INTO ai_conversations VALUES ('PRIVATE_LEGACY_CANARY')")
    before = curriculum["repo"].active_index("curriculum")
    with pytest.raises(ValueError, match="confirmation"):
        remove_legacy_conversations(path)
    # Disposable test database only. No migration is applied to a user database.
    remove_legacy_conversations(path, confirmed=True)
    remove_legacy_conversations(path, confirmed=True)
    assert curriculum["repo"].active_index("curriculum") == before
    with curriculum["repo"].connection() as con:
        assert con.execute("SELECT count FROM ai_daily_usage").fetchone()[0] == 1
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ai_conversations'"
        ).fetchone()
    assert b"PRIVATE_LEGACY_CANARY" not in path.read_bytes()
    assert ask(client_for(curriculum)).status_code == 200


def test_missing_context_still_retrieves_only_the_current_question(curriculum, monkeypatch):
    calls = []
    original = SemanticEmbeddings.embed_query

    def embed(self, text):
        calls.append(text)
        return original(self, text)

    monkeypatch.setattr(SemanticEmbeddings, "embed_query", embed)
    client = client_for(curriculum)
    questions = ["동명사", "왜 그런가요?", "Why?"]
    for question in questions:
        response = ask(client, question=question)
        assert response.status_code == 200
        if question != "동명사":
            assert response.json()["status"] == "clarification_needed"
    assert calls == questions

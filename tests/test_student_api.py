import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from firebase_admin import auth
from langchain_core.runnables import RunnableLambda

from vocard.api import create_app
from vocard.conversations import ConversationStore
from vocard.student_auth import FirebaseTokenVerifier

from .test_answering import draft
from .test_retrieval import SemanticEmbeddings
from .test_retrieval import curriculum as curriculum_fixture

curriculum = curriculum_fixture
QUESTION = {
    "question": "동명사",
    "conversation_id": None,
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


def test_verified_uid_owned_durable_conversations_and_bounded_history(curriculum):
    captured = []

    def generate(messages):
        captured.append(json.loads(messages[-1].content))
        return draft(messages)

    client = client_for(curriculum, generate, ai_history_messages=4)
    result = ask(client)
    assert result.status_code == 200, result.text
    cid = result.json()["conversation_id"]
    for _ in range(3):
        assert ask(client, conversation_id=cid, question="왜 그런가요?").status_code == 200
    assert len(captured[-1]["history"]) == 4
    saved = client.get(f"/ai/conversations/{cid}", headers={"Authorization": "Bearer alice"})
    assert saved.status_code == 200 and len(saved.json()["turns"]) == 2
    assert "uid" not in saved.json() and "lease_token" not in saved.json()
    reopened = client_for(curriculum)
    latest = reopened.get(
        "/ai/conversations",
        params={"course_id": "c1", "unit_id": "u1", "lesson_number": 1},
        headers={"Authorization": "Bearer alice"},
    )
    assert latest.json()["conversation"]["conversation_id"] == cid
    assert ask(client, uid="bob", conversation_id=cid).status_code == 404
    assert (
        client.get(f"/ai/conversations/{cid}", headers={"Authorization": "Bearer bob"}).status_code
        == 404
    )
    assert ask(client, conversation_id=cid, lesson_number=2).status_code == 409


def test_followups_reretrieve_new_active_evidence_not_prior_quotes(curriculum):
    captured = []

    def generate(messages):
        captured.append(json.loads(messages[-1].content))
        return draft(messages)

    client = client_for(curriculum, generate)
    first = ask(client).json()
    curriculum["ingest"](source=curriculum["pdf"] + b"\n")
    followup = ask(client, conversation_id=first["conversation_id"], question="왜 그런가요?")
    assert followup.status_code == 200, followup.text
    assert (
        first["citations"][0]["index_version"] != followup.json()["citations"][0]["index_version"]
    )
    assert captured[0]["evidence"][0]["chunk_id"] != captured[1]["evidence"][0]["chunk_id"]
    assert captured[1]["question"] == "왜 그런가요?"
    assert all("citations" not in h and "quote" not in h for h in captured[1]["history"])


def test_firebase_token_and_course_scope_are_server_controlled(curriculum):
    client = client_for(curriculum)
    assert client.post("/ai/answer", json=QUESTION).status_code == 401
    assert ask(client, uid="expired").status_code == 401
    assert ask(client, uid="anonymous").status_code == 403
    assert ask(client, course_id="foreign").status_code == 403
    missing = ask(client, lesson_number=999)
    assert missing.status_code == 200
    assert missing.json()["status"] == "insufficient_evidence"
    for extra in ({"uid": "victim"}, {"book_id": "other"}, {"history": []}):
        assert (
            client.post(
                "/ai/answer", json={**QUESTION, **extra}, headers={"Authorization": "Bearer alice"}
            ).status_code
            == 422
        )
    response = ask(client, uid="expired")
    assert "secret-token" not in response.text


def test_daily_quota_is_shared_across_conversations_and_survives_restart(curriculum):
    client = client_for(curriculum, ai_daily_quota=1)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 429
    assert ask(client, uid="bob").status_code == 200
    assert ask(client_for(curriculum, ai_daily_quota=1)).status_code == 429


def test_concurrent_request_leases_and_quota_are_atomic(curriculum):
    settings = replace(curriculum["settings"], ai_daily_quota=1)
    store = ConversationStore(curriculum["repo"], settings)
    scope = {"book_id": "curriculum", "course_id": "c1", "unit_id": "u1", "lesson_number": 1}

    def start(_):
        try:
            return store.begin(None, "alice", scope)
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, range(2)))
    assert sum(isinstance(r, tuple) for r in results) == 1 and 429 in results
    cid, lease, _ = next(r for r in results if isinstance(r, tuple))
    with pytest.raises(HTTPException) as exc:
        store.begin(cid, "alice", scope)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        store.complete(cid, "alice", "wrong-lease", [])
    store.release(cid, "alice", lease)


def test_failed_generation_does_not_append_history_or_hold_lease(curriculum):
    def broken(_):
        raise RuntimeError("private-provider-details")

    client = client_for(curriculum, broken)
    result = ask(client)
    assert result.status_code == 503 and "private-provider-details" not in result.text
    latest = client.get(
        "/ai/conversations",
        params={"course_id": "c1", "unit_id": "u1", "lesson_number": 1},
        headers={"Authorization": "Bearer alice"},
    )
    conversation = latest.json()["conversation"]
    assert conversation["turns"] == []
    recovered = ask(client_for(curriculum), conversation_id=conversation["conversation_id"])
    assert recovered.status_code == 200


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


def test_serialized_history_size_is_bounded_including_citation_metadata(curriculum):
    settings = replace(curriculum["settings"], ai_history_chars=1000)
    store = ConversationStore(curriculum["repo"], settings)
    scope = {"book_id": "curriculum", "course_id": "c1", "unit_id": "u1", "lesson_number": 1}
    cid, lease, _ = store.begin(None, "alice", scope)
    turns = [{"question": "질문", "response": {"answer": "답변", "citations": ["가" * 5000]}}]
    store.complete(cid, "alice", lease, turns)
    assert store.get(cid, "alice")["turns"] == []


def test_expired_request_cannot_overwrite_reclaimed_conversation(curriculum):
    store = ConversationStore(curriculum["repo"], curriculum["settings"])
    scope = {"book_id": "curriculum", "course_id": "c1", "unit_id": "u1", "lesson_number": 1}
    cid, old_lease, _ = store.begin(None, "alice", scope)
    with curriculum["repo"].connection(write=True) as con:
        con.execute("UPDATE ai_conversations SET lease_until=0 WHERE conversation_id=?", (cid,))
    _, new_lease, _ = store.begin(cid, "alice", scope)
    with pytest.raises(HTTPException) as error:
        store.complete(cid, "alice", old_lease, [])
    assert error.value.status_code == 409
    store.release(cid, "alice", old_lease)
    store.complete(
        cid, "alice", new_lease, [{"question": "새 질문", "response": {"answer": "새 답변"}}]
    )
    assert store.get(cid, "alice")["turns"][0]["question"] == "새 질문"

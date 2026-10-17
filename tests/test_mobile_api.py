import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from vocard.http_privacy import PrivateRequestBoundary
from vocard.mobile_api import grammar_documents, mobile_router
from vocard.student_api import StudentQuestion
from vocard.student_auth import FirebaseRestTokenVerifier

from .test_vocabulary import PAYLOAD, generated


class Usage:
    def __init__(self):
        self.calls = []

    def reserve(self, uid):
        self.calls.append(uid)


def client_for(output=None, verifier=None):
    class Model:
        async def ainvoke(self, messages):
            result = output or generated([SimpleNamespace(content=messages[-1][1])])
            return SimpleNamespace(text=json.dumps(result, ensure_ascii=False))

    usage = Usage()
    app = FastAPI()
    app.add_middleware(PrivateRequestBoundary)
    app.include_router(
        mobile_router(
            Model,
            verifier=verifier
            or (lambda token: {"uid": "student", "firebase": {"sign_in_provider": "google.com"}}),
            usage=usage,
        )
    )
    return TestClient(app), usage


AUTH = {"Authorization": "Bearer test-token"}


def test_vocabulary_real_contract_and_followups():
    client, usage = client_for()
    result = client.post("/ai/vocabulary/explain", json=PAYLOAD, headers=AUTH)
    assert result.status_code == 200
    data = result.json()
    assert data["status"] == "answered"
    assert data["suggested_follow_up_questions"]
    assert data["examples"][0]["label"] == "AI 생성 예문 (교재 원문 아님)"
    assert result.headers["cache-control"] == "no-store"
    assert "conversation_id" not in data
    assert usage.calls == ["student"]
    result = client.post(
        "/ai/vocabulary/explain",
        json={**PAYLOAD, "prior_explanation": data["contextual_meaning"]["explanation_ko"]},
        headers=AUTH,
    )
    assert result.status_code == 200


def test_auth_scope_and_sanitized_invalid_input():
    client, usage = client_for()
    assert client.post("/ai/vocabulary/explain", json=PAYLOAD).status_code == 401
    response = client.post(
        "/ai/vocabulary/explain", json={**PAYLOAD, "history": "secret"}, headers=AUTH
    )
    assert response.status_code == 422
    assert "secret" not in response.text
    unknown = {**PAYLOAD, "selected_entry": {**PAYLOAD["selected_entry"], "courseId": "other"}}
    assert client.post("/ai/vocabulary/explain", json=unknown, headers=AUTH).status_code == 403
    assert usage.calls == []
    client, _ = client_for(
        verifier=lambda _: {"uid": "anon", "firebase": {"sign_in_provider": "anonymous"}}
    )
    assert client.post("/ai/vocabulary/explain", json=PAYLOAD, headers=AUTH).status_code == 403


def test_invalid_model_output_never_reaches_client():
    client, _ = client_for(output={"answer": "private-invalid-provider-text"})
    result = client.post("/ai/vocabulary/explain", json=PAYLOAD, headers=AUTH)
    assert result.status_code == 502
    assert "private-invalid" not in result.text


def test_grammar_scopes_to_exact_asset_lesson_and_validates_citations():
    request = StudentQuestion(
        question="영어의 관점은?",
        course_id="devils_100",
        unit_id="devils-grammar-unit-01",
        lesson_number=1,
    )
    docs = grammar_documents(request)
    doc = next(iter(docs.values()))
    assert all(15 <= d.metadata["source_pages"][0] <= 18 for d in docs.values())
    output = {
        "answer": "영어는 외부에서 자신을 바라보는 관점을 설명합니다.",
        "status": "answered",
        "citations": [{"chunk_id": doc.id, "quote": doc.page_content[:80]}],
        "source_examples": [],
        "practice_examples": [],
    }
    client, _ = client_for(output)
    result = client.post("/ai/answer", json=request.model_dump(), headers=AUTH)
    assert result.status_code == 200
    assert result.json()["citations"][0]["book_id"] == "devils-100-ocr-draft"
    output["citations"][0]["quote"] = "invented quote"
    assert client.post("/ai/answer", json=request.model_dump(), headers=AUTH).status_code == 502
    assert (
        client.post(
            "/ai/answer", json={**request.model_dump(), "unit_id": "wrong-unit"}, headers=AUTH
        ).status_code
        == 404
    )


def test_hosted_chat_keeps_basic_auth_and_routes_students_by_firebase(monkeypatch):
    import vocard.mobile_api as mobile
    from vocard.chat import create_chat_app

    original = mobile.mobile_router
    monkeypatch.setattr(
        mobile,
        "mobile_router",
        lambda factory, **kw: original(
            factory, verifier=lambda _: {"uid": "student"}, usage=Usage(), **kw
        ),
    )
    monkeypatch.setenv("VOCARD_FIREBASE_PROJECT_ID", "vocard-6f1a5")
    monkeypatch.setenv("VOCARD_CHAT_USERNAME", "vocard")
    monkeypatch.setenv("VOCARD_CHAT_PASSWORD", "test-password-long-enough")
    client = TestClient(create_chat_app())
    assert client.get("/api/parts").status_code == 401
    assert client.post("/ai/answer", json={"question": "test"}).status_code == 401
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("change", ["revoked", "disabled", "identity", "issuer"])
def test_public_key_verifier_rejects_revocation_and_wrong_identity(change):
    claims = {"sub": "student", "iss": "https://securetoken.google.com/project", "auth_time": 100}
    user = {"localId": "student", "validSince": "90"}
    if change == "revoked":
        user["validSince"] = "110"
    if change == "disabled":
        user["disabled"] = True
    if change == "identity":
        user["localId"] = "other"
    if change == "issuer":
        claims["iss"] = "https://securetoken.google.com/other"
    verifier = FirebaseRestTokenVerifier(
        "project",
        "public-client-key",
        verify_token=lambda _: claims,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"users": [user]}))
        ),
    )
    with pytest.raises(HTTPException):
        verifier("token")


def test_public_key_verifier_validates_with_google_online():
    def lookup(request):
        assert request.url.host == "identitytoolkit.googleapis.com"
        assert json.loads(request.content) == {"idToken": "token"}
        return httpx.Response(200, json={"users": [{"localId": "student", "validSince": "90"}]})

    verifier = FirebaseRestTokenVerifier(
        "project",
        "public-client-key",
        verify_token=lambda _: {
            "sub": "student",
            "iss": "https://securetoken.google.com/project",
            "auth_time": 100,
        },
        client=httpx.Client(transport=httpx.MockTransport(lookup)),
    )
    assert verifier("token")["uid"] == "student"

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocard.expressions import catalog, expressions_router

AUTH = {"Authorization": "Bearer test-token"}
NOW = datetime(2026, 10, 23, 3, tzinfo=UTC)
CONTENT = Path(__file__).parents[1] / "vocard" / "daily_expressions.json"


def client_for(path=CONTENT, now=NOW, verifier=None):
    app = FastAPI()
    app.include_router(
        expressions_router(
            verifier=verifier or (lambda _: {"uid": "student"}), content_path=path, now=lambda: now
        )
    )
    return TestClient(app)


def test_today_matches_detail_and_feature():
    client = client_for()
    today = client.get("/expressions/today", headers=AUTH)
    assert today.status_code == 200
    assert today.headers["cache-control"] == "no-store"
    data = today.json()
    assert data["date"] == "2026-10-23"
    detail = client.get(f"/expression-lessons/{data['lesson_id']}", headers=AUTH).json()
    featured = next(
        item for item in detail["expressions"] if item["expression_id"] == data["expression_id"]
    )
    assert featured["english"] == data["english"]
    assert featured["korean"] == data["korean"]
    assert detail["featured_expression_id"] == data["expression_id"]
    assert len(detail["expressions"]) == 2
    assert detail["next_lesson_id"] is None
    assert "status" not in detail


def test_exclusive_pagination_no_duplicates_and_empty_page():
    client = client_for()
    page = client.get("/expression-lessons?limit=3", headers=AUTH).json()
    assert [item["date"] for item in page["items"]] == ["2026-10-23", "2026-10-22", "2026-10-21"]
    assert page["next_before"] == "2026-10-21"
    next_page = client.get(
        f"/expression-lessons?limit=3&before={page['next_before']}", headers=AUTH
    ).json()
    assert next_page["items"][0]["date"] == "2026-10-20"
    last = client.get(
        f"/expression-lessons?limit=3&before={next_page['next_before']}", headers=AUTH
    ).json()
    assert len(last["items"]) == 1
    assert last["next_before"] is None
    assert client.get("/expression-lessons?before=2020-01-01", headers=AUTH).json() == {
        "items": [],
        "next_before": None,
    }


def test_timezone_midnight_visibility_and_navigation():
    client = client_for(now=datetime(2026, 10, 22, 15, 1, tzinfo=UTC))
    assert (
        client.get("/expressions/today?timezone=Asia/Seoul", headers=AUTH).json()["date"]
        == "2026-10-23"
    )
    assert (
        client.get("/expressions/today?timezone=America/Los_Angeles", headers=AUTH).json()["date"]
        == "2026-10-22"
    )
    assert (
        client.get(
            "/expression-lessons/daily-2026-10-23?timezone=America/Los_Angeles", headers=AUTH
        ).status_code
        == 404
    )
    data = client.get(
        "/expression-lessons/daily-2026-10-22?timezone=America/Los_Angeles", headers=AUTH
    ).json()
    assert data["next_lesson_id"] is None
    assert data["previous_lesson_id"] == "daily-2026-10-21"
    first = client.get("/expression-lessons/daily-2026-10-17", headers=AUTH).json()
    assert first["previous_lesson_id"] is None


@pytest.mark.parametrize(
    "query",
    [
        "timezone=invalid",
        "timezone=../UTC",
        "limit=0",
        "limit=51",
        "limit=x",
        "before=no",
        "before=20261023",
    ],
)
def test_invalid_queries_have_stable_errors(query):
    result = client_for().get(f"/expression-lessons?{query}", headers=AUTH)
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "invalid_request"


def test_missing_today_does_not_substitute_yesterday():
    client = client_for(now=datetime(2026, 10, 25, tzinfo=UTC))
    assert client.get("/expressions/today", headers=AUTH).status_code == 404
    assert (
        client.get("/expression-lessons", headers=AUTH).json()["items"][0]["date"] == "2026-10-23"
    )


def test_authentication_and_anonymous_account():
    assert client_for().get("/expressions/today").status_code == 401
    anonymous = client_for(
        verifier=lambda _: {"uid": "guest", "firebase": {"sign_in_provider": "anonymous"}}
    )
    response = anonymous.get("/expression-lessons", headers=AUTH)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_not_eligible"
    invalid = client_for(verifier=lambda _: (_ for _ in ()).throw(ValueError("private-token")))
    response = invalid.get("/expression-lessons", headers=AUTH)
    assert response.status_code == 401
    assert "private-token" not in response.text


def test_draft_and_future_lessons_never_exposed(tmp_path):
    rows = json.loads(CONTENT.read_text())
    rows[-1]["status"] = "draft"
    path = tmp_path / "content.json"
    path.write_text(json.dumps(rows))
    client = client_for(path=path)
    assert client.get("/expressions/today", headers=AUTH).status_code == 404
    assert client.get("/expression-lessons/daily-2026-10-23", headers=AUTH).status_code == 404
    assert (
        client.get("/expression-lessons", headers=AUTH).json()["items"][0]["date"] == "2026-10-22"
    )


def test_invalid_or_missing_catalog_is_503(tmp_path):
    path = tmp_path / "content.json"
    assert client_for(path=path).get("/expressions/today", headers=AUTH).status_code == 503
    path.write_text("private-invalid-content")
    response = client_for(path=path).get("/expressions/today", headers=AUTH)
    assert response.status_code == 503
    assert "private-invalid-content" not in response.text
    rows = json.loads(CONTENT.read_text())
    path.write_text(json.dumps(rows + [rows[0]]))
    assert client_for(path=path).get("/expression-lessons", headers=AUTH).status_code == 503


def test_original_starter_content_validates():
    lessons = catalog(CONTENT)
    assert len(lessons) == 7
    for lesson in lessons:
        for expression in lesson.expressions:
            assert len(expression.dialogue) >= 2
            assert expression.audio_url is None


def test_render_router_uses_firebase_instead_of_browser_basic_auth(monkeypatch, tmp_path):
    import vocard.expressions as module
    from vocard.chat import create_chat_app

    monkeypatch.setenv("VOCARD_FIREBASE_PROJECT_ID", "vocard-test")
    monkeypatch.setenv("VOCARD_FIREBASE_WEB_API_KEY", "public-test-key")
    monkeypatch.setenv("VOCARD_CHAT_USERNAME", "browser")
    monkeypatch.setenv("VOCARD_CHAT_PASSWORD", "browser-password")
    monkeypatch.setenv("VOCARD_MOBILE_DB_PATH", str(tmp_path / "mobile.sqlite3"))
    monkeypatch.setattr(
        module, "FirebaseRestTokenVerifier", lambda *_: lambda _: {"uid": "student"}
    )
    client = TestClient(create_chat_app())
    assert client.get("/").status_code == 401
    response = client.get("/expression-lessons", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["items"]
    assert client.get("/expression-lessons").status_code == 401

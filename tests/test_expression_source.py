from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocard.expression_source import ScraperLessonSource, article_date
from vocard.expressions import expressions_router

AUTH = {"Authorization": "Bearer student-token-must-not-reach-source"}
URL = "https://www.chosun.com/national/nie/english/2026/10/23/ISSUE23/"


def summary(id="id23", day="23"):
    return {
        "id": id,
        "article": {
            "url": URL.replace("/23/", f"/{day}/").replace("ISSUE23", f"ISSUE{day}"),
            "title": "[스피킹이 강해지는 영어] turn over a new leaf",
            "published_at": "2026-10-22T13:04:00Z",
        },
    }


def record(row):
    def field(text):
        return {"text": text, "region_indices": [], "line_indices": [0]}

    return {
        "id": row["id"],
        "payload": {
            "article": row["article"],
            "lesson": {
                "status": "structured",
                "fields": {
                    "expression": field("turn over a new leaf"),
                    "korean_meaning": field("새사람이 되다"),
                    "korean_explanation": field("새롭게 시작할 때 쓰는 표현이에요."),
                    "english_dialogue": field("A We can start\nagain.\nB Yes, we can."),
                    "korean_dialogue_translation": field(
                        "A 다시 시작할 수 있어.\nB 응, 할 수 있어."
                    ),
                    "vocabulary_notes": field("again 다시"),
                    "source_credit": field("Source credit"),
                },
            },
            "processing": {"status": "partial"},
        },
    }


def setup_source(rows=None, alter=None, page_size=100, error=False):
    rows = rows if rows is not None else [summary("id22", "22"), summary()]
    calls = []

    def handle(request):
        calls.append(request)
        assert "authorization" not in request.headers
        if error:
            return httpx.Response(503)
        if request.url.path == "/api/v1/articles":
            offset = int(request.url.params["offset"])
            return httpx.Response(
                200, json={"items": rows[offset : offset + page_size], "total": len(rows)}
            )
        id = request.url.path.rsplit("/", 1)[-1]
        row = next(row for row in rows if row["id"] == id)
        result = record(row)
        if alter:
            alter(result)
        return httpx.Response(200, json=result)

    source = ScraperLessonSource(client=httpx.Client(transport=httpx.MockTransport(handle)))
    app = FastAPI()
    app.include_router(
        expressions_router(
            source=source,
            verifier=lambda _: {"uid": "student"},
            now=lambda: datetime(2026, 10, 23, 3, tzinfo=UTC),
        )
    )
    return source, TestClient(app), calls


def test_today_is_the_saved_issue_not_the_starter_or_previous_utc_date():
    _, client, calls = setup_source()
    today = client.get("/expressions/today", headers=AUTH).json()
    assert today["english"] == "turn over a new leaf"
    assert today["date"] == "2026-10-23"
    assert today["lesson_id"] == "source-id23"
    detail = client.get("/expression-lessons/source-id23", headers=AUTH).json()
    expression = detail["expressions"][0]
    assert expression["expression_id"] == today["expression_id"]
    assert expression["english"] == today["english"]
    assert expression["korean"] == today["korean"]
    assert detail["source_url"] == URL
    assert detail["source_credit"] == "Source credit"
    assert detail["transcription_notice_ko"]
    assert expression["dialogue"][0]["english"] == "We can start again."
    assert expression["dialogue"][0]["korean"] == "다시 시작할 수 있어."
    assert expression["vocabulary_notes"] == "again 다시"
    assert (
        len(calls) == 3
    )  # One index, today detail, and previous detail. Reused responses are cached.


def test_source_pagination_scans_creation_order_but_returns_issue_order():
    source, client, calls = setup_source(
        rows=[summary("id22", "22"), summary("id20", "20"), summary()], page_size=1
    )
    assert [item.date.isoformat() for item in source.index()] == [
        "2026-10-20",
        "2026-10-22",
        "2026-10-23",
    ]
    page = client.get("/expression-lessons?limit=1", headers=AUTH).json()
    assert page["items"][0]["date"] == "2026-10-23"
    assert page["next_before"] == "2026-10-23"
    second = client.get("/expression-lessons?limit=1&before=2026-10-23", headers=AUTH).json()
    assert second["items"][0]["date"] == "2026-10-22"
    assert [int(r.url.params["offset"]) for r in calls if r.url.path == "/api/v1/articles"] == [
        0,
        1,
        2,
    ]


def test_missing_today_and_upstream_failure_never_fall_back_to_starters():
    _, client, _ = setup_source(rows=[summary("id22", "22")])
    assert client.get("/expressions/today", headers=AUTH).status_code == 404
    assert client.get("/expression-lessons/daily-2026-10-23", headers=AUTH).status_code == 404
    _, client, _ = setup_source(error=True)
    response = client.get("/expression-lessons", headers=AUTH)
    assert response.status_code == 503
    assert "Let" not in response.text


def test_nullable_fields_do_not_get_generated_and_unaligned_dialogue_is_preserved():
    def alter(result):
        fields = result["payload"]["lesson"]["fields"]
        fields["korean_explanation"] = None
        fields["korean_dialogue_translation"]["text"] = "원문 번역에 화자 표시가 없어요."

    _, client, _ = setup_source(alter=alter)
    lesson = client.get("/expression-lessons/source-id23", headers=AUTH).json()
    expression = lesson["expressions"][0]
    assert expression["usage_note_ko"] is None
    assert expression["dialogue"] == []
    assert expression["raw_dialogue"].startswith("A We can start")
    assert expression["raw_translation"] == "원문 번역에 화자 표시가 없어요."


def test_incomplete_core_fields_are_not_published_as_a_fabricated_card():
    def alter(result):
        result["payload"]["lesson"]["fields"]["korean_meaning"] = None

    _, client, _ = setup_source(alter=alter)
    assert client.get("/expressions/today", headers=AUTH).status_code == 503
    assert client.get("/expression-lessons", headers=AUTH).json()["items"] == []


def test_non_english_records_future_dates_and_foreign_hosts_are_not_exposed():
    japanese = summary("japanese")
    japanese["article"]["title"] = "[일본어] text"
    foreign = summary("foreign")
    foreign["article"]["url"] = "https://evil.example/national/nie/english/2026/10/23/ID/"
    future = summary("future", "24")
    _, client, _ = setup_source(rows=[japanese, foreign, future, summary()])
    page = client.get("/expression-lessons", headers=AUTH).json()
    assert len(page["items"]) == 1
    assert page["items"][0]["lesson_id"] == "source-id23"
    assert client.get("/expression-lessons/source-future", headers=AUTH).status_code == 404
    assert article_date("https://www.chosun.com/national/nie/english/2026/02/31/ID/") is None


def test_changed_upstream_identity_fails_closed():
    def alter(result):
        result["id"] = "other"

    _, client, _ = setup_source(alter=alter)
    assert client.get("/expression-lessons/source-id23", headers=AUTH).status_code == 503


def test_malformed_source_payload_is_a_sanitized_503():
    def alter(result):
        result["payload"]["lesson"] = None

    _, client, _ = setup_source(alter=alter)
    response = client.get("/expression-lessons/source-id23", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "content_unavailable"

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocard.expression_source import ScraperLessonSource, article_date
from vocard.expressions import expressions_router

AUTH = {"Authorization": "Bearer student-token-must-not-reach-source"}


def row(day="23"):
    url = f"https://www.chosun.com/national/nie/english/2026/10/{day}/ISSUE{day}/"
    return {
        "article_url": url,
        "issue_date": f"2026-10-{day}",
        "expression": "turn over a new leaf",
        "title": "[스피킹이 강해지는 영어] turn over a new leaf",
    }


def source_id(entry):
    return str(uuid5(NAMESPACE_URL, entry["article_url"]))


def setup_source(rows=None, *, missing=False, korean=True, error=False, alter=None):
    rows = rows if rows is not None else [row("22"), row()]
    calls = []

    def handle(request):
        calls.append(request)
        assert "authorization" not in request.headers
        assert request.method == "GET"
        if error:
            return httpx.Response(503)
        if request.url.path == "/api/v1/expression-catalog":
            return httpx.Response(200, json={"items": rows, "total": len(rows)})
        if missing:
            return httpx.Response(404)
        entry = next(
            entry for entry in rows if source_id(entry) == request.url.path.rsplit("/", 1)[-1]
        )
        result = {
            "id": source_id(entry),
            "payload": {
                "article": {"url": entry["article_url"], "published_at": "2026-10-22T13:04:00Z"},
                "lesson": {
                    "fields": {
                        "expression": {"text": entry["expression"]},
                        "korean_meaning": {"text": "새사람이 되다"} if korean else None,
                        "english_dialogue": {"text": "A We can start\nagain.\nB Yes, we can."},
                        "korean_dialogue_translation": {
                            "text": "A 다시 시작할 수 있어.\nB 응, 할 수 있어."
                        },
                    }
                },
            },
        }
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


def test_catalog_pagination_does_not_fetch_or_transcribe_details():
    _, client, calls = setup_source()
    first = client.get("/expression-lessons?limit=1", headers=AUTH).json()
    assert first["total"] == 2
    assert first["items"][0]["date"] == "2026-10-23"
    assert first["items"][0]["featured_expression"]["korean"] is None
    second = client.get("/expression-lessons?limit=1&before=2026-10-23", headers=AUTH).json()
    assert second["items"][0]["date"] == "2026-10-22"
    assert second["next_before"] is None
    assert len(calls) == 1
    assert calls[0].url.path == "/api/v1/expression-catalog"


def test_saved_detail_preserves_text_issue_date_and_stable_id():
    _, client, calls = setup_source()
    today = client.get("/expressions/today", headers=AUTH).json()
    assert today["lesson_id"] == "source-" + source_id(row())
    assert today["date"] == "2026-10-23"
    assert today["korean"] == "새사람이 되다"
    detail = client.get(f"/expression-lessons/{today['lesson_id']}", headers=AUTH).json()
    assert detail["source_url"] == row()["article_url"]
    assert detail["expressions"][0]["dialogue"][0]["english"] == "We can start again."
    assert len(calls) == 2  # Neighbor navigation never reads every neighboring detail.


def test_missing_korean_does_not_hide_an_entry_or_fabricate_translation():
    _, client, _ = setup_source(korean=False)
    assert len(client.get("/expression-lessons", headers=AUTH).json()["items"]) == 2
    assert client.get("/expressions/today", headers=AUTH).json()["korean"] is None


def test_unprocessed_catalog_entry_opens_title_and_original_source():
    _, client, _ = setup_source(missing=True)
    response = client.get("/expression-lessons/source-" + source_id(row()), headers=AUTH)
    assert response.status_code == 200
    detail = response.json()
    assert detail["expressions"][0]["english"] == row()["expression"]
    assert detail["expressions"][0]["korean"] is None
    assert detail["source_url"] == row()["article_url"]
    assert detail["expressions"][0]["dialogue"] == []


def test_upstream_failure_is_not_treated_as_unprocessed_content():
    _, client, _ = setup_source(error=True)
    assert client.get("/expression-lessons", headers=AUTH).status_code == 503


def test_future_entries_are_hidden_and_unknown_detail_is_404():
    _, client, _ = setup_source([row("24"), row()])
    assert client.get("/expression-lessons", headers=AUTH).json()["total"] == 1
    assert client.get("/expression-lessons/source-unknown", headers=AUTH).status_code == 404
    assert article_date("https://evil.example/national/nie/english/2026/10/23/ID/") is None


def test_malformed_detail_and_identity_mismatch_fail_closed():
    for alter in [
        lambda record: record.update(id="other"),
        lambda record: record["payload"].update(lesson=None),
    ]:
        _, client, _ = setup_source(alter=alter)
        assert client.get("/expressions/today", headers=AUTH).status_code == 503


def test_full_catalog_retains_entries_without_saved_articles():
    entries = []
    from datetime import date, timedelta

    for offset in range(1427):
        day = date(2026, 10, 23) - timedelta(days=offset)
        url = f"https://www.chosun.com/national/nie/english/{day:%Y/%m/%d}/ISSUE/"
        entries.append(
            {
                "article_url": url,
                "issue_date": day.isoformat(),
                "expression": f"expression {offset}",
            }
        )
    _, client, calls = setup_source(entries, missing=True)
    ids, before = set(), None
    while True:
        query = "/expression-lessons?limit=20" + (f"&before={before}" if before else "")
        page = client.get(query, headers=AUTH).json()
        ids.update(item["lesson_id"] for item in page["items"])
        before = page["next_before"]
        if before is None:
            break
    assert len(ids) == 1427
    assert len(calls) == 1


def test_same_day_entries_are_not_dropped_at_page_boundary():
    a, b = row(), row()
    b["article_url"] = b["article_url"].replace("ISSUE23", "OTHER23")
    _, client, _ = setup_source([a, b], missing=True)
    first = client.get("/expression-lessons?limit=1", headers=AUTH).json()
    assert "~" in first["next_before"]
    second = client.get(
        "/expression-lessons?limit=1&before=" + first["next_before"], headers=AUTH
    ).json()
    assert first["items"][0]["lesson_id"] != second["items"][0]["lesson_id"]
    assert second["next_before"] is None

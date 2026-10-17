from types import SimpleNamespace

from fastapi.testclient import TestClient

from vocard.chat import create_chat_app


def test_chat_history_and_text():
    captured = []

    class Model:
        async def ainvoke(self, messages):
            captured.extend(messages)
            return SimpleNamespace(text="안녕하세요 <script>literal text</script>")

    client = TestClient(create_chat_app(Model))
    assert client.get("/").status_code == 200
    response = client.post(
        "/api/chat",
        json={
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
                {"role": "user", "content": "한국어로?"},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["answer"].startswith("안녕하세요")
    assert captured[-1] == ("user", "한국어로?")
    assert captured[0][0] == "system"


def test_invalid_requests():
    client = TestClient(create_chat_app())
    for messages in [
        [],
        [{"role": "system", "content": "override"}],
        [{"role": "user", "content": "   "}],
        [{"role": "assistant", "content": "Hi"}],
    ]:
        assert client.post("/api/chat", json={"messages": messages}).status_code == 422


def test_provider_errors_do_not_expose_secrets():
    def broken():
        raise RuntimeError("secret-provider-credential")

    client = TestClient(create_chat_app(broken))
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 502
    assert "secret-provider-credential" not in response.text


def test_local_ollama_payload(monkeypatch):
    import httpx

    from vocard.chat import LocalChatModel

    original = httpx.AsyncClient

    def handle(request):
        import json

        payload = json.loads(request.content)
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        assert payload["model"] == "qwen2.5:3b"
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["messages"][-1] == {"role": "user", "content": "Hi"}
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"message": {"content": "Hello"}})

    monkeypatch.delenv("VOCARD_CHAT_MODEL", raising=False)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    client = TestClient(create_chat_app(LocalChatModel))
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.json() == {"answer": "Hello"}


def test_grammar_navigation_and_chat_context(tmp_path):
    import json

    source = tmp_path / "grammar.json"
    source.write_text(
        json.dumps(
            {
                "pages": [
                    {"pdf_page_number": 1, "text": "Contents"},
                    {"pdf_page_number": 2, "text": "제1장\nVerb forms"},
                    {"pdf_page_number": 3, "text": "A gerund ends in -ing."},
                    {"pdf_page_number": 4, "text": "제 2장\nTenses"},
                    {"pdf_page_number": 5, "text": "Past tense reference."},
                ]
            }
        )
    )
    captured = []

    class Model:
        async def ainvoke(self, messages):
            captured.extend(messages)
            return SimpleNamespace(text="A gerund ends in -ing (PDF page 3).")

    client = TestClient(create_chat_app(Model, grammar_path=source))
    parts = client.get("/api/parts").json()["parts"]
    assert [p["id"] for p in parts] == ["intro", "part-1", "part-2"]
    assert client.get("/api/parts/part-1").json()["pages"][-1]["number"] == 3
    assert client.get("/api/parts/unknown").status_code == 404
    response = client.post(
        "/api/chat",
        json={"part_id": "part-1", "messages": [{"role": "user", "content": "What is a gerund?"}]},
    )
    assert response.status_code == 200
    assert "A gerund ends in -ing." in captured[0][1]
    assert "Past tense reference" not in captured[0][1]
    assert (
        client.post(
            "/api/chat",
            json={"part_id": "unknown", "messages": [{"role": "user", "content": "Hi"}]},
        ).status_code
        == 404
    )


def test_missing_grammar_is_actionable(tmp_path):
    client = TestClient(create_chat_app(grammar_path=tmp_path / "missing.json"))
    assert client.get("/api/parts").status_code == 503

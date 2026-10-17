from types import SimpleNamespace

from fastapi.testclient import TestClient

from vocard.chat import create_chat_app


def test_independent_question_and_text():
    captured = []

    class Model:
        async def ainvoke(self, messages):
            captured.extend(messages)
            return SimpleNamespace(text="안녕하세요 <script>literal text</script>")

    client = TestClient(create_chat_app(Model))
    assert client.get("/").status_code == 200
    response = client.post(
        "/api/chat",
        json={"question": "한국어로?"},
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
    response = client.post("/api/chat", json={"question": "Hi"})
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
    response = client.post("/api/chat", json={"question": "Hi"})
    assert response.json()["answer"] == "Hello"


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
        json={"part_id": "part-1", "question": "What is a gerund?"},
    )
    assert response.status_code == 200
    assert "A gerund ends in -ing." in captured[0][1]
    assert "Past tense reference" not in captured[0][1]
    assert (
        client.post(
            "/api/chat",
            json={"part_id": "unknown", "question": "Hi"},
        ).status_code
        == 404
    )


def test_missing_grammar_is_actionable(tmp_path):
    client = TestClient(create_chat_app(grammar_path=tmp_path / "missing.json"))
    assert client.get("/api/parts").status_code == 503


def test_hosted_gemini_auth_and_payload(monkeypatch):
    import json

    import httpx

    monkeypatch.setenv("VOCARD_CHAT_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret-key")
    monkeypatch.setenv("VOCARD_CHAT_USERNAME", "vocard")
    monkeypatch.setenv("VOCARD_CHAT_PASSWORD", "test-long-password")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "vocard-test.onrender.com")
    calls = []
    original = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        assert request.headers["x-goog-api-key"] == "test-secret-key"
        assert "test-secret-key" not in str(request.url)
        payload = json.loads(request.content)
        assert payload["systemInstruction"]["parts"][0]["text"].startswith("You are Vocard")
        assert [c["role"] for c in payload["contents"]] == ["user"]
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "Hello Gemini"}]}}]}
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    client = TestClient(create_chat_app(), base_url="https://vocard-test.onrender.com")
    assert client.get("/healthz").status_code == 200
    for path in ["/", "/api/parts", "/api/parts/part-1"]:
        assert client.get(path).status_code == 401
    body = {"question": "Again"}
    assert client.post("/api/chat", json=body).status_code == 401
    assert client.post("/api/chat", json=body, auth=("vocard", "bad")).status_code == 401
    assert not calls
    response = client.post("/api/chat", json=body, auth=("vocard", "test-long-password"))
    assert response.json()["answer"] == "Hello Gemini"
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_hosted_configuration_fails_closed(monkeypatch):
    import pytest

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("VOCARD_CHAT_USERNAME", raising=False)
    monkeypatch.delenv("VOCARD_CHAT_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="requires a username"):
        create_chat_app()


def test_provider_quota_error_is_safe():
    import httpx

    class Model:
        async def ainvoke(self, messages):
            response = httpx.Response(
                429,
                request=httpx.Request("POST", "https://example.com"),
                text="secret-provider-details",
            )
            response.raise_for_status()

    client = TestClient(create_chat_app(Model))
    response = client.post("/api/chat", json={"question": "Hi"})
    assert response.status_code == 429
    assert "secret" not in response.text


def test_chat_runs_without_langchain_or_langsmith():
    """Exercise the Render app factory without the full API's optional SDKs."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import importlib.abc
        import os
        import sys
        from types import SimpleNamespace

        class BlockInferenceSDKs(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in {'langchain_core', 'langsmith'}:
                    raise ModuleNotFoundError(fullname)

        sys.meta_path.insert(0, BlockInferenceSDKs())
        os.environ.update(
            RENDER='true',
            VOCARD_CHAT_PROVIDER='gemini',
            VOCARD_CHAT_USERNAME='vocard',
            VOCARD_CHAT_PASSWORD='test-long-password',
            GEMINI_API_KEY='test-key',
        )
        from fastapi.testclient import TestClient
        from vocard.chat import create_chat_app

        class Model:
            async def ainvoke(self, messages):
                return SimpleNamespace(text='Hello')

        with TestClient(create_chat_app(Model)) as client:
            assert client.get('/healthz').status_code == 200
            auth = ('vocard', 'test-long-password')
            response = client.post('/api/chat', json={'question': 'Hi'}, auth=auth)
            assert response.json()['answer'] == 'Hello'
            assert len(response.json()['follow_up_questions']) == 3
            assert response.headers['cache-control'] == 'no-store'
            response = client.post('/api/chat', json={'question': ''}, auth=auth)
            assert response.status_code == 422
            assert response.json() == {'detail': 'Invalid question request'}
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_gemini_http_errors_show_safe_configuration_hints(monkeypatch):
    import httpx

    monkeypatch.setenv("VOCARD_CHAT_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret-key")
    monkeypatch.setenv("VOCARD_CHAT_USERNAME", "vocard")
    monkeypatch.setenv("VOCARD_CHAT_PASSWORD", "test-long-password")

    for status, hint in [
        (400, "input size"),
        (401, "GEMINI_API_KEY"),
        (403, "permissions"),
        (404, "VOCARD_GEMINI_MODEL"),
        (500, "temporarily unavailable"),
        (503, "temporarily unavailable"),
        (418, "Check server configuration"),
    ]:

        class Model:
            async def ainvoke(self, messages, status=status):
                httpx.Response(
                    status,
                    request=httpx.Request("POST", "https://example.com"),
                    text="secret-provider-details test-secret-key private-question",
                ).raise_for_status()

        with TestClient(create_chat_app(Model)) as client:
            response = client.post(
                "/api/chat",
                json={"question": "private-question"},
                auth=("vocard", "test-long-password"),
            )
        assert response.status_code == 502
        assert f"HTTP {status}" in response.json()["detail"]
        assert hint in response.json()["detail"]
        assert "secret" not in response.text
        assert "private-question" not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_generated_followups_are_returned_by_chat():
    import json

    questions = [
        "When does a gerund follow enjoy?",
        "How does a gerund differ from an infinitive?",
        "Can you give a practice question about gerunds?",
    ]

    class Model:
        async def ainvoke(self, messages):
            assert "exactly three distinct questions" in messages[0][1]
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "answer": "Use a gerund after enjoy.",
                        "follow_up_questions": questions,
                    }
                )
            )

    with TestClient(create_chat_app(Model)) as client:
        response = client.post("/api/chat", json={"question": "Explain gerunds"})
    assert response.json() == {
        "answer": "Use a gerund after enjoy.",
        "follow_up_questions": questions,
    }


def test_followups_are_guaranteed_for_unstructured_or_incomplete_output():
    import json

    from vocard.chat import chat_response

    for text in [
        "Plain explanation",
        '{"answer": "Explanation", "follow_up_questions": null}',
        json.dumps({"answer": "Explanation", "follow_up_questions": ["Why?", "Why?", 7, ""]}),
        '```json\n{"answer":"Explanation","follow_up_questions":[]}\n```',
    ]:
        result = chat_response(text, "동명사를 설명해 주세요", "동명사")
        questions = result["follow_up_questions"]
        assert len(questions) == len(set(questions)) == 3
        assert any("동명사" in question for question in questions)
        assert any("연습 문제" in question for question in questions)
        assert result["answer"] in {"Plain explanation", "Explanation"}


def test_hosted_ollama_endpoint_and_auth(monkeypatch):
    import httpx
    import json

    original = httpx.AsyncClient
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("VOCARD_CHAT_PROVIDER", "ollama")
    monkeypatch.setenv("VOCARD_CHAT_USERNAME", "vocard")
    monkeypatch.setenv("VOCARD_CHAT_PASSWORD", "test-long-password")
    monkeypatch.setenv("VOCARD_OLLAMA_BASE_URL", "https://ollama.com/")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret-cloud-key")
    monkeypatch.setenv("VOCARD_CHAT_MODEL", "test-cloud-model")

    def handle(request):
        assert str(request.url) == "https://ollama.com/api/chat"
        assert request.headers["authorization"] == "Bearer secret-cloud-key"
        assert json.loads(request.content)["model"] == "test-cloud-model"
        return httpx.Response(200, json={"message": {"content": "Hello"}})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    client = TestClient(create_chat_app())
    assert client.get("/healthz").status_code == 200
    assert client.post("/api/chat", json={"question": "Hi"}).status_code == 401
    response = client.post(
        "/api/chat", json={"question": "Hi"}, auth=("vocard", "test-long-password")
    )
    assert response.json()["answer"] == "Hello"
    assert "secret-cloud-key" not in response.text


def test_hosted_ollama_rejects_missing_key_or_insecure_endpoint(monkeypatch):
    import pytest

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("VOCARD_CHAT_PROVIDER", "ollama")
    monkeypatch.setenv("VOCARD_CHAT_USERNAME", "vocard")
    monkeypatch.setenv("VOCARD_CHAT_PASSWORD", "test-long-password")
    monkeypatch.setenv("VOCARD_OLLAMA_BASE_URL", "https://ollama.com")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Hosted Ollama requires"):
        create_chat_app()
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    monkeypatch.setenv("VOCARD_OLLAMA_BASE_URL", "http://localhost:11434")
    with pytest.raises(ValueError, match="Hosted Ollama requires"):
        create_chat_app()

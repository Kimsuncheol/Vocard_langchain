import json
import os
import socket
import subprocess
import sys
import threading
import time

import uvicorn

from vocard.api import create_app

from .conftest import TestEmbeddings, make_pdf, token


def test_cli_to_live_http_api_and_durable_worker(system, tmp_path):
    app = create_app(
        system["settings"], system["repo"], system["storage"], lambda _: TestEmbeddings()
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(make_pdf("Must 7 Gerunds Where am I? English grammar examples."))
        env = {**os.environ, "VOCARD_ADMIN_TOKEN": token(system["settings"])}
        command = [sys.executable, "-m", "vocard.cli", "--api-url", f"http://127.0.0.1:{port}"]
        upload = subprocess.run(
            command + ["ingest", "cli-book", str(pdf)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert upload.returncode == 0, upload.stderr
        assert '"status": "queued"' in upload.stdout
        assert system["repo"].book("cli-book")["active_index_version"] is None
        assert system["worker"].run_once()
        exported = tmp_path / "reviewed.json"
        export = subprocess.run(
            command
            + ["extraction", json.loads(upload.stdout)["job_id"], "--output", str(exported)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert export.returncode == 0, export.stderr
        assert json.loads(exported.read_text())["pages"][0]["pdf_page_index"] == 1
        approved = subprocess.run(
            command
            + [
                "ingest",
                "cli-book",
                str(pdf),
                "--extraction-json",
                str(exported),
                "--reviewed-extraction",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert approved.returncode == 0, approved.stderr
        assert json.loads(approved.stdout)["reviewed_by"] == "test-admin"
        assert system["worker"].run_once()
        search = subprocess.run(
            command
            + [
                "search",
                "cli-book",
                "Where am I?",
                "--current-lesson",
                "7",
                "--no-book-fallback",
                "--top-k",
                "1",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert search.returncode == 0, search.stderr
        assert "Where am I?" in search.stdout
        assert json.loads(search.stdout)["documents"][0]["metadata"]["lesson_number"] == 7
        lookup = subprocess.run(
            command + ["lesson", "cli-book", "--lesson-number", "7"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert lookup.returncode == 0, lookup.stderr
        assert json.loads(lookup.stdout)["total_chunks"] == 1
        denied_env = {**env, "VOCARD_ADMIN_TOKEN": token(system["settings"], role="reader")}
        denied = subprocess.run(
            command + ["book", "cli-book"],
            env=denied_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert denied.returncode == 1 and "403" in denied.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_cli_refuses_remote_cleartext_token_transport(system):
    result = subprocess.run(
        [sys.executable, "-m", "vocard.cli", "--api-url", "http://example.com", "book", "book"],
        env={**os.environ, "VOCARD_ADMIN_TOKEN": "not-sent"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "HTTPS" in result.stderr

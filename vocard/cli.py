import argparse
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description="Administrator book ingestion CLI")
    parser.add_argument("--api-url", default=os.getenv("VOCARD_API_URL", "http://127.0.0.1:8000"))
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Upload a PDF and enqueue a durable job")
    ingest.add_argument("book_id")
    ingest.add_argument("pdf", type=Path)
    ingest.add_argument("--extraction-json", type=Path)
    ingest.add_argument("--course-id")
    ingest.add_argument("--unit-id")
    ingest.add_argument("--chunk-size", type=int)
    ingest.add_argument(
        "--allow-uncertain",
        action="store_true",
        help="Deprecated: does not bypass administrator review",
    )
    ingest.add_argument(
        "--reviewed-extraction",
        action="store_true",
        help="Attest that the supplied extracted JSON has been reviewed",
    )
    extraction = commands.add_parser("extraction", help="Export a saved extraction for review")
    extraction.add_argument("job_id")
    extraction.add_argument("--output", type=Path, required=True)
    commands.add_parser("job").add_argument("job_id")
    commands.add_parser("book").add_argument("book_id")
    commands.add_parser("retry").add_argument("job_id")
    search = commands.add_parser("search")
    search.add_argument("book_id")
    search.add_argument("query")
    search.add_argument("--top-k", "--k", dest="top_k", type=int)
    search.add_argument("--current-lesson", type=int)
    search.add_argument("--current-lesson-title")
    search.add_argument("--course-id")
    search.add_argument("--unit-id")
    search.add_argument("--no-book-fallback", action="store_true")
    search.add_argument("--max-fallback", type=int)
    search.add_argument("--min-score", type=float)
    lesson = commands.add_parser("lesson", help="Exact lookup by lesson number or title")
    lesson.add_argument("book_id")
    lesson.add_argument("--lesson-number", type=int)
    lesson.add_argument("--title")
    lesson.add_argument("--course-id")
    lesson.add_argument("--unit-id")
    lesson.add_argument("--limit", type=int, default=50)
    lesson.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    token = os.getenv("VOCARD_ADMIN_TOKEN")
    if not token:
        parser.error("Set VOCARD_ADMIN_TOKEN to an administrator JWT")
    url = httpx.URL(args.api_url)
    if url.scheme != "https" and not (
        url.scheme == "http" and url.host in ("localhost", "127.0.0.1", "::1")
    ):
        parser.error("Use HTTPS for remote API connections")
    try:
        with (
            httpx.Client(
                base_url=args.api_url.rstrip("/"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=300,
                follow_redirects=False,
            ) as client,
            ExitStack() as files,
        ):
            if args.command == "ingest":
                source = files.enter_context(args.pdf.open("rb"))
                uploads = {"file": (args.pdf.name, source, "application/pdf")}
                if args.extraction_json:
                    sidecar = files.enter_context(args.extraction_json.open("rb"))
                    uploads["extraction"] = (args.extraction_json.name, sidecar, "application/json")
                response = client.post(
                    f"/admin/books/{args.book_id}/ingestions",
                    files=uploads,
                    data={
                        "allow_uncertain": str(args.allow_uncertain).lower(),
                        "reviewed_extraction": str(args.reviewed_extraction).lower(),
                        **{
                            key: getattr(args, key)
                            for key in ("course_id", "unit_id", "chunk_size")
                            if getattr(args, key) is not None
                        },
                    },
                )
            elif args.command == "extraction":
                response = client.get(f"/admin/jobs/{args.job_id}/extraction")
            elif args.command == "job":
                response = client.get(f"/admin/jobs/{args.job_id}")
            elif args.command == "book":
                response = client.get(f"/admin/books/{args.book_id}")
            elif args.command == "retry":
                response = client.post(f"/admin/jobs/{args.job_id}/retry")
            elif args.command == "lesson":
                response = client.get(
                    f"/admin/books/{args.book_id}/lessons",
                    params={
                        key: getattr(args, key)
                        for key in (
                            "lesson_number",
                            "title",
                            "course_id",
                            "unit_id",
                            "limit",
                            "offset",
                        )
                        if getattr(args, key) is not None
                    },
                )
            else:
                response = client.post(
                    f"/admin/books/{args.book_id}/search",
                    json={
                        "query": args.query,
                        "allow_book_fallback": not args.no_book_fallback,
                        **{
                            key: getattr(args, key)
                            for key in (
                                "top_k",
                                "current_lesson",
                                "current_lesson_title",
                                "course_id",
                                "unit_id",
                                "max_fallback",
                                "min_score",
                            )
                            if getattr(args, key) is not None
                        },
                    },
                )
            response.raise_for_status()
            if args.command == "extraction":
                # Exclusive creation prevents accidentally replacing a reviewed local artifact.
                with args.output.open("xb") as output:
                    output.write(response.content)
                print(str(args.output))
            else:
                print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    except (OSError, httpx.HTTPError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            print(f"API returned {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
        else:
            print(f"Request failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

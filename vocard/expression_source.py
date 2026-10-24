"""Read persisted English lessons. Never scrape, generate, or forward student tokens."""

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx


@dataclass(frozen=True)
class LessonIndex:
    lesson_id: str
    date: date
    title: str
    source_id: str
    source_url: str


def article_date(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"chosun.com", "www.chosun.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = re.fullmatch(r"/national/nie/(?:english/)?(\d{4})/(\d{2})/(\d{2})/[^/]+/?", parsed.path)
    try:
        return date(*map(int, match.groups())) if match else None
    except ValueError:
        return None


def field(fields, name):
    value = fields.get(name)
    text = value.get("text") if isinstance(value, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else None


def turns(text):
    result = []
    for line in (text or "").splitlines():
        match = re.match(r"^\s*([A-Z])(?:\s*[:：.)-]\s*|\s+)(\S.*)$", line)
        if match:
            result.append([match[1], match[2]])
        elif line.strip() and result:
            result[-1][1] += " " + line.strip()
        elif line.strip():
            return []
    return result


def convert_lesson(index, record):
    if not isinstance(record, dict) or record.get("id") != index.source_id:
        raise ValueError("Source identity mismatch")
    payload = record["payload"]
    if not isinstance(payload, dict):
        raise ValueError("Invalid source payload")
    article = payload["article"]
    if not isinstance(article, dict):
        raise ValueError("Invalid source article")
    if str(article.get("url")) != index.source_url:
        raise ValueError("Source URL mismatch")
    lesson = payload["lesson"]
    if not isinstance(lesson, dict) or not isinstance(lesson.get("fields"), dict):
        raise ValueError("Invalid source lesson fields")
    fields = lesson["fields"]
    english, korean = field(fields, "expression"), field(fields, "korean_meaning")
    english = english or index.title
    # Missing transcription must not erase a publisher-listed expression.
    original_english = field(fields, "english_dialogue")
    original_korean = field(fields, "korean_dialogue_translation")
    en, ko = turns(original_english), turns(original_korean)
    aligned = bool(en) and [turn[0] for turn in en] == [turn[0] for turn in ko]
    expression_id = index.lesson_id + "-expression"
    return {
        "lesson_id": index.lesson_id,
        "date": index.date.isoformat(),
        "title": english,
        "status": "published",
        "featured_expression_id": expression_id,
        "source_url": index.source_url,
        "source_name": "조선일보 · 스피킹이 강해지는 영어",
        "source_credit": field(fields, "source_credit"),
        "transcription_notice_ko": "원문 이미지 인식 결과예요. 인식 오류가 있을 수 있어요.",
        "expressions": [
            {
                "expression_id": expression_id,
                "english": english,
                "korean": korean,
                "usage_note_ko": field(fields, "korean_explanation"),
                "dialogue": [
                    {"speaker": a[0], "english": a[1], "korean": b[1]}
                    for a, b in zip(en, ko, strict=True)
                ]
                if aligned
                else [],
                "raw_dialogue": original_english if not aligned else None,
                "raw_translation": original_korean if not aligned else None,
                "vocabulary_notes": field(fields, "vocabulary_notes"),
                "audio_url": None,
            }
        ],
    }


class ScraperLessonSource:
    def __init__(self, base_url="https://english-scrapper.onrender.com", client=None, ttl=300):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid expression source URL")
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=90, trust_env=False, follow_redirects=False)
        self.ttl = ttl
        self._index = None
        self._index_until = 0
        self._details = OrderedDict()
        self._lock = threading.RLock()

    def _json(self, path, params=None):
        # Public saved-content reads only. No Authorization header is forwarded.
        with self.client.stream("GET", self.base_url + path, params=params) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("Expression source response too large")
        import json

        return json.loads(body)

    def index(self):
        with self._lock:
            if self._index is not None and time.monotonic() < self._index_until:
                return self._index
            catalog = self._json("/api/v1/expression-catalog")
            if not isinstance(catalog, dict):
                raise ValueError("Invalid expression catalog")
            items, total = catalog.get("items"), catalog.get("total")
            if (
                not isinstance(items, list)
                or type(total) is not int
                or not 0 <= total <= 4000
                or len(items) != total
            ):
                raise ValueError("Incomplete expression catalog")
            rows, seen = [], set()
            for item in items:
                url = item.get("article_url") if isinstance(item, dict) else None
                issue = article_date(url) if isinstance(url, str) else None
                title = item.get("expression") if isinstance(item, dict) else None
                if (
                    issue is None
                    or not isinstance(title, str)
                    or not title.strip()
                    or item.get("issue_date") != issue.isoformat()
                ):
                    raise ValueError("Invalid catalog entry")
                source_id = str(uuid5(NAMESPACE_URL, url))
                if source_id in seen:
                    raise ValueError("Duplicate catalog entry")
                seen.add(source_id)
                rows.append(
                    LessonIndex("source-" + source_id, issue, title.strip(), source_id, url)
                )
            self._index = sorted(rows, key=lambda item: (item.date, item.lesson_id))
            self._index_until = time.monotonic() + self.ttl
            return self._index

    def summary(self, item):
        return {
            "lesson_id": item.lesson_id,
            "date": item.date.isoformat(),
            "title": item.title,
            "expression_count": 1,
            "featured_expression": {
                "expression_id": item.lesson_id + "-expression",
                "english": item.title,
                "korean": None,
            },
        }

    def lesson(self, item):
        with self._lock:
            cached = self._details.get(item.lesson_id)
            if cached and cached[0] > time.monotonic():
                self._details.move_to_end(item.lesson_id)
                return cached[1]
            try:
                record = self._json("/api/v1/articles/" + quote(item.source_id, safe=""))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                # A catalog title exists before its full lesson is transcribed.
                record = {
                    "id": item.source_id,
                    "payload": {"article": {"url": item.source_url}, "lesson": {"fields": {}}},
                }
            lesson = convert_lesson(item, record)
            self._details[item.lesson_id] = (time.monotonic() + self.ttl, lesson)
            if len(self._details) > 64:
                self._details.popitem(last=False)
            return lesson

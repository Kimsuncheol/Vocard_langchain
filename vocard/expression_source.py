"""Read persisted English lessons. Never scrape, generate, or forward student tokens."""

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlsplit

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
    if not english or not korean:
        return None  # Incomplete transcription is not a publishable study card.
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
    def __init__(self, base_url="https://english-scrapper.onrender.com", client=None, ttl=60):
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
            rows = []
            offset = 0
            for _ in range(50):
                page = self._json("/api/v1/articles", {"limit": 100, "offset": offset})
                if not isinstance(page, dict):
                    raise ValueError("Invalid source page")
                items, total = page["items"], page["total"]
                if not isinstance(items, list) or type(total) is not int or total < 0:
                    raise ValueError("Invalid source page")
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("article"), dict):
                        raise ValueError("Invalid source article summary")
                    article = item["article"]
                    title = article.get("title") or ""
                    url = str(article.get("url") or "")
                    issue = article_date(url)
                    source_id = item.get("id", "")
                    if (
                        issue is None
                        or not isinstance(title, str)
                        or not isinstance(source_id, str)
                        or not title.startswith("[스피킹이 강해지는 영어]")
                        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", source_id)
                    ):
                        continue
                    rows.append(LessonIndex("source-" + source_id, issue, title, source_id, url))
                offset += len(items)
                if offset >= total:
                    break
                if not items:
                    raise ValueError("Incomplete source pagination")
            else:
                raise ValueError("Source index exceeds configured bounds")
            # Scrape insertion order is not publication order. Dates come from the issue URL,
            # not UTC article timestamps (which may be on the previous date).
            by_date = {}
            for item in sorted(rows, key=lambda row: (row.date, row.lesson_id)):
                by_date.setdefault(item.date, item)
            self._index = sorted(by_date.values(), key=lambda item: item.date)
            self._index_until = time.monotonic() + self.ttl
            return self._index

    def lesson(self, item):
        with self._lock:
            cached = self._details.get(item.lesson_id)
            if cached and cached[0] > time.monotonic():
                self._details.move_to_end(item.lesson_id)
                return cached[1]
            record = self._json("/api/v1/articles/" + quote(item.source_id, safe=""))
            lesson = convert_lesson(item, record)
            self._details[item.lesson_id] = (time.monotonic() + self.ttl, lesson)
            if len(self._details) > 64:
                self._details.popitem(last=False)
            return lesson

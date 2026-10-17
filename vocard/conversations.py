"""Durable, UID-owned, bounded conversation turns with fenced request leases."""

import json
import time
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException


class ConversationStore:
    def __init__(self, repository, settings):
        self.repository, self.settings = repository, settings
        with repository.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS ai_conversations (
                    conversation_id TEXT PRIMARY KEY, uid TEXT NOT NULL,
                    book_id TEXT NOT NULL, course_id TEXT NOT NULL,
                    unit_id TEXT NOT NULL, lesson_number INTEGER NOT NULL,
                    turns_json TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL,
                    lease_token TEXT, lease_until REAL
                );
                CREATE INDEX IF NOT EXISTS ai_conversation_owner
                    ON ai_conversations(uid,course_id,unit_id,lesson_number,updated_at);
                CREATE TABLE IF NOT EXISTS ai_daily_usage (
                    uid TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(uid,day)
                );
            """)

    @staticmethod
    def _owned(con, conversation_id, uid):
        row = con.execute(
            "SELECT * FROM ai_conversations WHERE conversation_id=? AND uid=?",
            (conversation_id, uid),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Conversation not found")
        return dict(row)

    def get(self, conversation_id, uid):
        with self.repository.connection() as con:
            row = self._owned(con, conversation_id, uid)
        return {
            k: v
            for k, v in row.items()
            if k
            not in (
                "uid",
                "turns_json",
                "lease_token",
                "lease_until",
            )
        } | {"turns": json.loads(row["turns_json"])}

    def latest(self, uid, book_id, course_id, unit_id, lesson_number):
        with self.repository.connection() as con:
            row = con.execute(
                "SELECT conversation_id FROM ai_conversations WHERE uid=? AND book_id=? "
                "AND course_id=? AND unit_id=? AND lesson_number=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (uid, book_id, course_id, unit_id, lesson_number),
            ).fetchone()
        return self.get(row["conversation_id"], uid) if row else None

    def begin(self, conversation_id, uid, scope):
        now, lease = time.time(), str(uuid.uuid4())
        day = datetime.now(UTC).date().isoformat()
        with self.repository.connection(write=True) as con:
            if conversation_id:
                row = self._owned(con, conversation_id, uid)
                if any(row[k] != v for k, v in scope.items()):
                    raise HTTPException(409, "Conversation belongs to another lesson")
                if row["lease_token"] and row["lease_until"] > now:
                    raise HTTPException(409, "A question is already in progress")
            else:
                conversation_id = str(uuid.uuid4())
                con.execute(
                    "INSERT INTO ai_conversations "
                    "(conversation_id,uid,book_id,course_id,unit_id,lesson_number,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        conversation_id,
                        uid,
                        scope["book_id"],
                        scope["course_id"],
                        scope["unit_id"],
                        scope["lesson_number"],
                        now,
                    ),
                )
                row = {"turns_json": "[]"}
            usage = con.execute(
                "SELECT count FROM ai_daily_usage WHERE uid=? AND day=?", (uid, day)
            ).fetchone()
            if usage and usage["count"] >= self.settings.ai_daily_quota:
                raise HTTPException(429, "Daily AI quota reached", headers={"Retry-After": "3600"})
            con.execute(
                "INSERT INTO ai_daily_usage VALUES (?,?,1) "
                "ON CONFLICT(uid,day) DO UPDATE SET count=count+1",
                (uid, day),
            )
            con.execute(
                "UPDATE ai_conversations SET lease_token=?,lease_until=? "
                "WHERE conversation_id=? AND uid=?",
                (lease, now + 900, conversation_id, uid),
            )
        return conversation_id, lease, json.loads(row["turns_json"])

    def release(self, conversation_id, uid, lease):
        with self.repository.connection(write=True) as con:
            con.execute(
                "UPDATE ai_conversations SET lease_token=NULL,lease_until=NULL "
                "WHERE conversation_id=? AND uid=? AND lease_token=?",
                (conversation_id, uid, lease),
            )

    def complete(self, conversation_id, uid, lease, turns):
        # Keep complete exchanges, not a dangling assistant message.
        turns = turns[-(self.settings.ai_history_messages // 2) :]

        def history_size(items):
            return len(json.dumps(items, ensure_ascii=False))

        while turns and history_size(turns) > self.settings.ai_history_chars:
            turns.pop(0)
        with self.repository.connection(write=True) as con:
            changed = con.execute(
                "UPDATE ai_conversations SET turns_json=?,updated_at=?,"
                "lease_token=NULL,lease_until=NULL WHERE conversation_id=? AND uid=? "
                "AND lease_token=? AND lease_until>?",
                (
                    json.dumps(turns, ensure_ascii=False),
                    time.time(),
                    conversation_id,
                    uid,
                    lease,
                    time.time(),
                ),
            ).rowcount
            if not changed:
                raise HTTPException(409, "Conversation request expired; retry")

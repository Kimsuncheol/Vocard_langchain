"""Atomic per-UID daily counters. Never accepts question or answer content."""

from datetime import UTC, datetime

from fastapi import HTTPException


class UsageCounters:
    def __init__(self, repository, settings):
        self.repository, self.settings = repository, settings
        with repository.connection(write=True) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS ai_daily_usage ("
                "uid TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, "
                "PRIMARY KEY(uid,day))"
            )
            legacy = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_conversations'"
            ).fetchone()
            if legacy and con.execute("SELECT 1 FROM ai_conversations LIMIT 1").fetchone():
                raise RuntimeError(
                    "Legacy conversation data exists. Complete the documented privacy "
                    "migration before starting the stateless API."
                )

    def reserve(self, uid):
        day = datetime.now(UTC).date().isoformat()
        with self.repository.connection(write=True) as con:
            row = con.execute(
                "SELECT count FROM ai_daily_usage WHERE uid=? AND day=?", (uid, day)
            ).fetchone()
            if row and row["count"] >= self.settings.ai_daily_quota:
                raise HTTPException(429, "Daily AI quota reached", headers={"Retry-After": "3600"})
            con.execute(
                "INSERT INTO ai_daily_usage VALUES (?,?,1) "
                "ON CONFLICT(uid,day) DO UPDATE SET count=count+1",
                (uid, day),
            )

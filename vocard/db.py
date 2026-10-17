import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class LeaseLost(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    book_id TEXT PRIMARY KEY,
    active_index_version TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS book_versions (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(book_id),
    book_version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    source_key TEXT NOT NULL,
    extraction_status TEXT NOT NULL DEFAULT 'queued'
        CHECK(extraction_status IN ('queued','running','succeeded','failed')),
    created_at REAL NOT NULL,
    UNIQUE(book_id, sha256),
    UNIQUE(book_id, book_version)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    book_version_id TEXT NOT NULL REFERENCES book_versions(id),
    sequence INTEGER NOT NULL,
    pipeline_fingerprint TEXT NOT NULL,
    pipeline_json TEXT NOT NULL,
    extraction_key TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','succeeded','failed')),
    extraction_status TEXT NOT NULL DEFAULT 'queued'
        CHECK(extraction_status IN ('queued','running','succeeded','failed')),
    stage TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    available_at REAL NOT NULL,
    lease_token TEXT,
    lease_until REAL,
    last_error TEXT,
    publication_result TEXT,
    requested_by TEXT NOT NULL,
    last_retried_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(book_version_id, pipeline_fingerprint)
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS index_versions (
    index_version TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
    book_id TEXT NOT NULL REFERENCES books(book_id),
    sequence INTEGER NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    previous_index_version TEXT,
    created_at REAL NOT NULL
);
"""


class Repository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("Unsupported database schema version")
            con.executescript(SCHEMA)
            # Additive migration: preserve existing books, active indexes and queued jobs.
            con.execute("BEGIN IMMEDIATE")
            try:
                columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
                additions = {
                    "extracted_key": "TEXT",
                    "extracted_sha256": "TEXT",
                    "review_status": "TEXT NOT NULL DEFAULT 'none'",
                    "review_report_json": "TEXT",
                    "reviewed_by": "TEXT",
                    "reviewed_at": "REAL",
                }
                for column, definition in additions.items():
                    if column not in columns:
                        con.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
                con.execute("PRAGMA user_version=2")
                con.commit()
            except BaseException:
                con.rollback()
                raise
        path.chmod(0o600)

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def enqueue(self, book_id, sha, source_key, extraction_key, pipeline, actor, max_attempts):
        payload = json.dumps(pipeline, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        now = time.time()
        with self.connection(write=True) as con:
            con.execute(
                "INSERT OR IGNORE INTO books(book_id,created_at) VALUES (?,?)", (book_id, now)
            )
            version = con.execute(
                "SELECT * FROM book_versions WHERE book_id=? AND sha256=?", (book_id, sha)
            ).fetchone()
            if version is None:
                number = con.execute(
                    "SELECT COALESCE(MAX(book_version),0)+1 FROM book_versions WHERE book_id=?",
                    (book_id,),
                ).fetchone()[0]
                version_id = str(uuid.uuid4())
                con.execute(
                    "INSERT INTO book_versions(id,book_id,book_version,sha256,source_key,"
                    "created_at) VALUES (?,?,?,?,?,?)",
                    (version_id, book_id, number, sha, source_key, now),
                )
            else:
                version_id = version["id"]
            existing = con.execute(
                "SELECT job_id FROM jobs WHERE book_version_id=? AND pipeline_fingerprint=?",
                (version_id, fingerprint),
            ).fetchone()
            if existing:
                job_id, created = existing[0], False
            else:
                sequence = con.execute(
                    "SELECT COALESCE(MAX(j.sequence),0)+1 FROM jobs j JOIN book_versions v "
                    "ON v.id=j.book_version_id WHERE v.book_id=?",
                    (book_id,),
                ).fetchone()[0]
                job_id, created = str(uuid.uuid4()), True
                con.execute(
                    "INSERT INTO jobs(job_id,book_version_id,sequence,pipeline_fingerprint,"
                    "pipeline_json,extraction_key,max_attempts,available_at,requested_by,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        version_id,
                        sequence,
                        fingerprint,
                        payload,
                        extraction_key,
                        max_attempts,
                        now,
                        actor,
                        now,
                        now,
                    ),
                )
                if pipeline.get("reviewed_extraction"):
                    con.execute(
                        "UPDATE jobs SET reviewed_by=?,reviewed_at=? WHERE job_id=?",
                        (actor, now, job_id),
                    )
        return {**self.job(job_id), "created": created}

    def job(self, job_id):
        with self.connection() as con:
            row = con.execute(
                "SELECT j.*,v.book_id,v.book_version,v.sha256,v.source_key,"
                "v.extraction_status AS book_extraction_status "
                "FROM jobs j JOIN book_versions v ON v.id=j.book_version_id WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["review_report"] = (
            json.loads(result["review_report_json"]) if result["review_report_json"] else None
        )
        return result

    def book(self, book_id):
        with self.connection() as con:
            row = con.execute("SELECT * FROM books WHERE book_id=?", (book_id,)).fetchone()
            if row is None:
                raise KeyError(book_id)
            versions = con.execute(
                "SELECT * FROM book_versions WHERE book_id=? ORDER BY book_version", (book_id,)
            ).fetchall()
            indexes = con.execute(
                "SELECT * FROM index_versions WHERE book_id=? ORDER BY sequence", (book_id,)
            ).fetchall()
        return {
            **dict(row),
            "versions": list(map(dict, versions)),
            "indexes": list(map(dict, indexes)),
        }

    def claim(self, lease_seconds):
        now, token = time.time(), str(uuid.uuid4())
        with self.connection(write=True) as con:
            exhausted = con.execute(
                "SELECT * FROM jobs WHERE status='running' AND lease_until<=? "
                "AND attempts>=max_attempts",
                (now,),
            ).fetchall()
            for row in exhausted:
                con.execute(
                    "UPDATE jobs SET status='failed',last_error='Worker lease expired; "
                    "retry limit reached',extraction_status=CASE WHEN extraction_status='running' "
                    "THEN 'failed' ELSE extraction_status END,"
                    "lease_token=NULL,lease_until=NULL,updated_at=? "
                    "WHERE job_id=?",
                    (now, row["job_id"]),
                )
                con.execute(
                    "UPDATE book_versions SET extraction_status='failed' WHERE id=? "
                    "AND extraction_status='running'",
                    (row["book_version_id"],),
                )
            row = con.execute(
                "SELECT job_id FROM jobs WHERE attempts<max_attempts AND "
                "((status='queued' AND available_at<=?) OR "
                "(status='running' AND lease_until<=?)) ORDER BY created_at,job_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            con.execute(
                "UPDATE jobs SET status='running',stage='extracting',attempts=attempts+1,"
                "lease_token=?,lease_until=?,updated_at=? WHERE job_id=?",
                (token, now + lease_seconds, now, row[0]),
            )
        return self.job(row[0])

    @staticmethod
    def owned(con, job_id, token):
        row = con.execute(
            "SELECT * FROM jobs WHERE job_id=? AND status='running' AND lease_token=? "
            "AND lease_until>?",
            (job_id, token, time.time()),
        ).fetchone()
        if row is None:
            raise LeaseLost("Worker lease lost")
        return row

    def heartbeat(self, job_id, token, lease_seconds):
        with self.connection(write=True) as con:
            self.owned(con, job_id, token)
            con.execute(
                "UPDATE jobs SET lease_until=? WHERE job_id=?",
                (time.time() + lease_seconds, job_id),
            )

    def stage(self, job_id, token, stage, extraction_status=None):
        with self.connection(write=True) as con:
            job = self.owned(con, job_id, token)
            con.execute(
                "UPDATE jobs SET stage=?,updated_at=? WHERE job_id=?", (stage, time.time(), job_id)
            )
            if extraction_status:
                con.execute(
                    "UPDATE jobs SET extraction_status=? WHERE job_id=?",
                    (extraction_status, job_id),
                )
                condition = (
                    ""
                    if extraction_status == "succeeded"
                    else (" AND extraction_status!='succeeded'")
                )
                con.execute(
                    "UPDATE book_versions SET extraction_status=? WHERE id=?" + condition,
                    (extraction_status, job["book_version_id"]),
                )

    def fail(self, job_id, token, error, permanent=False):
        with self.connection(write=True) as con:
            job = self.owned(con, job_id, token)
            retry = not permanent and job["attempts"] < job["max_attempts"]
            con.execute(
                "UPDATE jobs SET status=?,last_error=?,available_at=?,lease_token=NULL,"
                "lease_until=NULL,updated_at=? WHERE job_id=?",
                (
                    "queued" if retry else "failed",
                    error[:1000],
                    time.time() + min(300, 2 ** job["attempts"]),
                    time.time(),
                    job_id,
                ),
            )
            if job["stage"] == "extracting":
                con.execute(
                    "UPDATE jobs SET extraction_status=? WHERE job_id=?",
                    ("queued" if retry else "failed", job_id),
                )
                con.execute(
                    "UPDATE book_versions SET extraction_status=? WHERE id=? "
                    "AND extraction_status!='succeeded'",
                    ("queued" if retry else "failed", job["book_version_id"]),
                )

    def retry(self, job_id, actor):
        with self.connection(write=True) as con:
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "failed":
                raise ValueError("Only failed jobs can be explicitly retried")
            if row["review_status"] == "required":
                raise ValueError("Export the extraction and submit reviewed JSON for this PDF hash")
            con.execute(
                "UPDATE jobs SET status='queued',stage='queued',available_at=?,"
                "max_attempts=attempts+3,last_retried_by=?,updated_at=? WHERE job_id=?",
                (time.time(), actor, time.time(), job_id),
            )
        return self.job(job_id)

    def record_extraction(self, job_id, token, key, sha, report):
        with self.connection(write=True) as con:
            job = self.owned(con, job_id, token)
            required = report["required"]
            status = "required" if required else "approved" if job["reviewed_by"] else "none"
            con.execute(
                "UPDATE jobs SET extracted_key=?,extracted_sha256=?,review_status=?,"
                "review_report_json=?,extraction_status='succeeded',updated_at=? WHERE job_id=?",
                (key, sha, status, json.dumps(report), time.time(), job_id),
            )
            con.execute(
                "UPDATE book_versions SET extraction_status='succeeded' WHERE id=?",
                (job["book_version_id"],),
            )
            if required:
                con.execute(
                    "UPDATE jobs SET status='failed',stage='review_required',"
                    "last_error='Extraction requires administrator review',"
                    "lease_token=NULL,lease_until=NULL WHERE job_id=?",
                    (job_id,),
                )
        return required

    def publish(self, job_id, token, key, sha, validation):
        """Fenced atomic promotion. Old immutable indexes are never deleted."""
        if validation.get("passed") is not True:
            raise ValueError("Cannot publish without successful validation")
        with self.connection(write=True) as con:
            job = self.owned(con, job_id, token)
            if job["stage"] != "validating":
                raise ValueError("Job is not ready for publication")
            version = con.execute(
                "SELECT * FROM book_versions WHERE id=?", (job["book_version_id"],)
            ).fetchone()
            book = con.execute(
                "SELECT * FROM books WHERE book_id=?", (version["book_id"],)
            ).fetchone()
            old = book["active_index_version"]
            old_sequence = -1
            if old:
                old_sequence = con.execute(
                    "SELECT sequence FROM index_versions WHERE index_version=?", (old,)
                ).fetchone()[0]
            index_version = str(uuid.uuid4())
            con.execute(
                "INSERT INTO index_versions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    index_version,
                    job_id,
                    version["book_id"],
                    job["sequence"],
                    key,
                    sha,
                    json.dumps(validation),
                    old,
                    time.time(),
                ),
            )
            promoted = job["sequence"] > old_sequence
            if promoted:
                con.execute(
                    "UPDATE books SET active_index_version=? WHERE book_id=?",
                    (index_version, version["book_id"]),
                )
            con.execute(
                "UPDATE jobs SET status='succeeded',stage='completed',publication_result=?,"
                "lease_token=NULL,lease_until=NULL,last_error=NULL,updated_at=? WHERE job_id=?",
                ("published" if promoted else "superseded", time.time(), job_id),
            )
        return index_version

    def active_index(self, book_id):
        with self.connection() as con:
            row = con.execute(
                "SELECT i.*,j.pipeline_json FROM books b JOIN index_versions i "
                "ON i.index_version=b.active_index_version JOIN jobs j ON j.job_id=i.job_id "
                "WHERE b.book_id=?",
                (book_id,),
            ).fetchone()
        if row is None:
            raise KeyError("Book has no active index")
        return dict(row)

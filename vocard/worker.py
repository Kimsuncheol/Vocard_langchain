import argparse
import hashlib
import io
import json
import logging
import signal
import threading

from vocard.config import Settings
from vocard.db import LeaseLost, Repository
from vocard.ingestion import InputRejected
from vocard.pipeline import build_index, embeddings_for, extract_pages, split_pages, validate_index
from vocard.storage import PrivateS3

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, repository, storage, embedding_factory=embeddings_for):
        self.settings, self.repository, self.storage = settings, repository, storage
        self.embedding_factory = embedding_factory

    def run_once(self) -> bool:
        job = self.repository.claim(self.settings.lease_seconds)
        if job is None:
            return False
        job_id, token = job["job_id"], job["lease_token"]
        stop, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not stop.wait(self.settings.lease_seconds / 3):
                try:
                    self.repository.heartbeat(job_id, token, self.settings.lease_seconds)
                except Exception:
                    lost.set()
                    logger.warning(
                        "Heartbeat failed for job %s; publication will be fenced", job_id
                    )
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self.repository.stage(job_id, token, "extracting", "running")
            pipeline = json.loads(job["pipeline_json"])
            pdf = self.storage.get(job["source_key"], job["sha256"])
            sidecar = (
                self.storage.get(job["extraction_key"], pipeline["extraction_sha256"])
                if job["extraction_key"]
                else None
            )
            pages = extract_pages(pdf, job, sidecar)
            self.repository.stage(job_id, token, "indexing", "succeeded")
            chunks = split_pages(pages, job)
            if lost.is_set():
                raise LeaseLost("Heartbeat failed")
            embeddings = self.embedding_factory(pipeline)
            index = build_index(chunks, job, embeddings)
            payload = json.dumps(
                index, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            sha = hashlib.sha256(payload).hexdigest()
            # Each attempt has its own immutable key; a stale worker cannot replace another build.
            key = f"indexes/{job['book_id']}/{job_id}/{token}.json"
            self.storage.put(key, io.BytesIO(payload), sha, "application/json")
            self.repository.stage(job_id, token, "validating")
            stored = json.loads(self.storage.get(key, sha))
            report = validate_index(stored, chunks, job, embeddings)
            if lost.is_set():
                raise LeaseLost("Heartbeat failed")
            version = self.repository.publish(job_id, token, key, sha, report)
            logger.info("Completed job %s, index version %s", job_id, version)
        except LeaseLost:
            logger.warning("Abandoning stale attempt for job %s", job_id)
        except Exception as exc:
            # Do not put provider responses, credentials or document content in status responses.
            message = str(exc) if isinstance(exc, InputRejected) else type(exc).__name__
            logger.warning(
                "Job %s failed at %s: %s", job_id, self.repository.job(job_id)["stage"], message
            )
            try:
                self.repository.fail(
                    job_id, token, message, permanent=isinstance(exc, InputRejected)
                )
            except LeaseLost:
                logger.warning("Failure update fenced for job %s", job_id)
        finally:
            stop.set()
            thread.join(timeout=5)
        return True


def main():
    parser = argparse.ArgumentParser(description="Run the durable book ingestion worker")
    parser.add_argument("--once", action="store_true", help="Claim at most one currently ready job")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    repository, storage = Repository(settings.db_path), PrivateS3(settings.bucket)
    storage.assert_private()
    worker = Worker(settings, repository, storage)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    while not stopping.is_set():
        worked = worker.run_once()
        if args.once:
            break
        if not worked:
            stopping.wait(2)


if __name__ == "__main__":
    main()

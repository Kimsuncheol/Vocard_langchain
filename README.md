# Vocard administrator book ingestion

FastAPI accepts administrator-authorized uploads and records durable jobs. A separate
worker uses LangChain documents, text splitting, and OpenAI embeddings to build an
immutable index in private S3. A LangChain `BaseRetriever` resolves the active index
at query time. There is no administrator dashboard or in-request ingestion worker.

## Install and configure

Use Python 3.11 or newer (tested with Python 3.14):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps -e .
cp .env.example .env
```

`requirements.txt` pins the tested environment, including test tools;
`requirements.in` declares runtime dependencies for the installable package.
Edit `.env`, then export its values in both service environments, for example
`set -a; source .env; set +a` in a trusted local shell. The application does not
implicitly load environment files. Docker Compose reads `.env` itself.

Required configuration:

- `VOCARD_S3_BUCKET`: an existing private AWS S3 bucket. The optional CloudFormation
  template in `infrastructure/private-bucket.yaml` creates encrypted storage with
  versioning, TLS-only access, and all four Block Public Access settings enabled.
  Nothing in this repository automatically creates or changes cloud resources.
- AWS credentials through the standard boto3 credential chain, preferably a service
  IAM role. Each service requires `s3:GetBucketPublicAccessBlock` and
  `s3:GetBucketPolicyStatus` on the bucket. The API requires `s3:PutObject` and
  `s3:GetObject` on `sources/*` and `extractions/*`, plus `s3:GetObject` on `indexes/*`
  for search. The worker requires `s3:GetObject` on all three prefixes and
  `s3:PutObject` on `indexes/*` and `extractions/generated/*`. No public ACL, public URL, deletion, or list
  permission is needed. The bucket check runs at startup and before each operation;
  missing permissions or unsafe configuration fail closed.
- `VOCARD_JWT_SECRET`: at least 32 random bytes shared with your trusted JWT issuer.
  API tokens must use HS256 and contain `sub`, `iat`, `exp`, matching `iss` and
  `aud`, and exactly `role: "administrator"`. Defaults are issuer `vocard-admin`
  and audience `vocard-ingestion`. End users must never receive the signing secret.
  The application deliberately has no token-minting or role-assignment endpoint.
- `OPENAI_API_KEY` in the worker and API search environment. Document text is sent
  to OpenAI for embeddings. Model and dimensions are snapshotted per job/index;
  search uses that snapshot, including when the service defaults change.

Set `VOCARD_ADMIN_TOKEN` to a short-lived administrator JWT issued by your trusted
identity service. CLI requests use this token; the CLI cannot bypass authorization
by accessing the queue or database directly. Authentication runs before multipart
body parsing. Remote CLI connections require HTTPS. Put an HTTPS reverse proxy
with request limits in front of a remotely exposed API.

## Run the API and worker independently

In separate terminals with the same configuration and persistent database path:

```sh
.venv/bin/uvicorn vocard.api:create_app --factory --host 127.0.0.1 --port 8000
```

```sh
.venv/bin/python -m vocard.worker
```

Alternatively, `docker compose up --build -d` runs both processes with restart
policies and a shared persistent SQLite volume. Use service-role credentials or
explicitly provide AWS credential configuration to the containers. `--once` on
the worker claims at most one currently available job; omit it for a service.
SIGTERM finishes the current attempt before exiting. A killed worker's lease
expires and another worker can recover the job.

SQLite uses WAL, full synchronization, transactions, and a persistent file. Run
the API and workers on one host with the same local disk/volume; do not put the
database on NFS or copy it independently to multiple hosts. Back it up using
SQLite's online backup mechanism; copying only the main file while WAL is active
is insufficient. Multi-host deployments need a transactional queue/database
adapter such as PostgreSQL. Worker processes and database files belong to the
trusted service boundary; there is no unauthenticated remote worker interface.

## CLI

```sh
.venv/bin/vocard ingest grammar-book /absolute/path/book.pdf
.venv/bin/vocard job JOB_ID
.venv/bin/vocard book grammar-book
.venv/bin/vocard retry FAILED_JOB_ID
.venv/bin/vocard search grammar-book 'Where am I?' --k 4
```

The ingest command returns immediately after private uploads and the database
transaction, with `job_id`, `book_id`, PDF `sha256`, numeric `book_version`,
`extraction_status`, and queue status. `book` returns the active index version,
source version history, and immutable index history. Failed-job retry is explicit
and audited by administrator subject; re-uploading the same failed input returns
the same failed job rather than spending more embedding requests automatically.

## Korean/English extraction and review

The worker first extracts embedded PDF text. Empty, short, corrupt, symbol-heavy,
or suspicious text falls back **per page** to PDFium rendering and local Tesseract
OCR with both `kor` and `eng`. Image pages with little embedded text also use OCR.
Docker installs both language models. For macOS install Tesseract and its language
models (`brew install tesseract tesseract-lang`); for Debian/Ubuntu use
`apt-get install tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng`. A custom directory
containing `kor.traineddata` and `eng.traineddata` can be configured with
`VOCARD_TESSDATA_DIR`. OCR never calls an LLM or external OCR service.

OCR DPI, timeout, pixel limit, and confidence threshold are configured in
`.env.example`. Missing models, renderer failures, OCR timeouts, low confidence,
empty pages, unreadable glyphs, and source uncertainty are recorded for review.
All automatic OCR drafts require review, including high-confidence output;
confidence alone does not guarantee faithful Korean text, punctuation or markers.
OCR word positions and confidences remain in the saved JSON. Original spelling,
incorrect example sentences, and punctuation are not repaired or completed.

Every extraction is saved privately at `extractions/generated/<job>/<attempt>.json`
before embedding. A blocked job reports `status: "failed"`, `stage: "review_required"`,
`review_status: "required"`, and a per-page `review_report`. The previous active
index stays unchanged. These jobs do not auto-retry or call the embedding provider.
Use the authenticated CLI to export the draft and review it against the PDF:

```sh
.venv/bin/vocard extraction JOB_ID --output /absolute/path/reviewed-book.json
```

The output path must not already exist, preventing accidental replacement of a
reviewed file. Edit this JSON to match the source, then explicitly attest review
when importing it with the same PDF:

```sh
.venv/bin/vocard ingest grammar-book /absolute/path/book.pdf \
  --extraction-json /absolute/path/reviewed-book.json --reviewed-extraction
```

The administrator identity, review time, PDF SHA-256, and imported JSON SHA-256
are recorded. The import must match the exact PDF hash and cover every PDF page
exactly once in order. Embedded `reviewed_by` or approval claims are ignored;
only the authenticated `--reviewed-extraction` submission records approval. The
old `--allow-uncertain` flag is deprecated and **cannot bypass review**. Reviewed
uncertainty flags stay in the artifact and retrieval metadata. An empty reviewed
page must explicitly set `"review_disposition": "blank"`; a completely empty book
never publishes. A reviewed import still passes all index validation checks.

The existing `output/pdf/textbook-extraction/textbook.json` is an unreviewed OCR
draft and can be used as an import reference. Review it before passing
`--reviewed-extraction`; do not treat its existing uncertainty flags as approval.
The original PDF and raw imported JSON are retained as immutable private objects.

### Grammar JSON and page identity

The canonical schema is `schema/grammar-extraction.schema.json`, generated from
`vocard.extraction.GrammarExtraction` and based on the existing Vocard grammar JSON.
It retains headings, explanations, examples, translations, example pairs, source
lines, unknown source fields, raw correctness markers, and boolean or null
`is_correct`. `false` is preserved and is never inferred from grammaticality.
Structured grammar fields also survive chunking in retrieval metadata.

New output uses **1-based** `pdf_page_index` and `pdf_page_number`, independently
of `printed_page_labels`. For example, PDF page 15 may contain printed label
`"16"`, or a front-matter page may contain `"iv"`. Printed labels are never used
to calculate or replace PDF page indexes. Automatic edge-label detection is
marked uncertain; imported labels are preserved verbatim.

```json
{
  "source": {"sha256": "SHA-256 of the exact uploaded PDF bytes", "page_index_base": 1},
  "pages": [{
    "pdf_page_index": 1,
    "text": "× I enjoyed to swim.\n난 수영을 즐겼다.",
    "printed_page_labels": [{"text": "16", "uncertain": false}],
    "explanations": [{"text": "동명사에 대한 원문의 설명"}],
    "examples": [{
      "text": "× I enjoyed to swim.",
      "is_correct": false,
      "translations": ["난 수영을 즐겼다."],
      "correct_incorrect_markers": [{"raw": "×"}]
    }]
  }]
}
```

The legacy zero-based JSON is accepted with `source.page_index_base: 0`; an
omitted base is accepted only when the complete index sequence unambiguously
starts at 0 or 1. Mixed, duplicated, missing or inconsistent indexes are rejected.
Legacy indexes are preserved as `source_pdf_page_index` and
`source_page_index_base` while the canonical output and new retrieval metadata
use one-based indexes. Existing published indexes remain immutable and retain
the metadata format they were originally built with. The additive schema-v2
SQLite migration preserves source versions, queued jobs and active pointers.


## Durability, idempotency, and publication

1. Hash upload bytes and conditionally store the PDF at
   `sources/sha256/<sha256>.pdf` with server-side AES256 encryption. OCR sidecars
   are also content-addressed. No upload is overwritten. An interrupted upload
   or enqueue may leave an unreferenced object, which cannot become active.
2. Under a database write transaction, deduplicate a book version by
   `(book_id, source SHA-256)` and a job by `(book version, pipeline fingerprint)`.
   The fingerprint includes extraction SHA-256, model, dimensions, chunk settings,
   review attestation, OCR configuration, limits, and explicit pipeline revision. Increment
   `VOCARD_PIPELINE_REVISION` when changing extraction or indexing behavior.
   Concurrent duplicate submissions return the same job and source version.
3. A separate worker claims a persisted job using an expiring, heartbeated lease.
   Every mutation checks the lease token and expiry. Transient failures retry with
   bounded exponential backoff; malformed inputs and validation failures stop
   immediately. Claims, retry count, stage, extraction status, timestamps, requesting
   administrator, and last safe error are persisted. Expired final attempts become
   failed; explicit administrator retry grants three additional attempts.
4. Build at an attempt-specific immutable `indexes/<book>/<job>/<lease>.json` key.
   Retry attempts can repeat extraction/embedding work (at-least-once execution),
   but cannot overwrite a competing attempt or publish twice. No Python pickle
   or unsafe vector-store deserialization is used.
5. Verify that chunk source spans cover every extracted character without gaps.
   Download the stored index and validate its checksum, exact documents and metadata,
   source identity, chunk count, unique IDs, finite nonzero vectors, dimensions,
   model configuration, and query/document embedding consistency. The query smoke
   test requires cosine similarity of at least 0.95 for identical input; this tests
   pipeline consistency, not semantic retrieval quality or OCR accuracy.
6. In one fenced transaction, record the validation report and new immutable index
   version, switch `books.active_index_version`, and complete the job. Failure before
   this transaction leaves the active pointer untouched. The old index, validation
   report, and previous-version link remain available. An older queued job finishing
   after a newer publication is recorded as `superseded` and cannot roll back the
   active pointer. In-flight queries finish against the snapshot they already read.

Embedding a large textbook may take time and incur provider costs. Live ingestion
does not happen during installation, tests, or upload submission. JSON vector
artifacts and cosine retrieval are intended for a modest book library; large
collections should use a vector database behind the same publication boundary.

## Verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check vocard tests
.venv/bin/ruff format --check vocard tests
```

Tests use mocked S3 and deterministic test-only embeddings, with no real AWS or
OpenAI credentials. They cover authorization, concurrent idempotency, private
encrypted storage, durable queue reopening, crash recovery and fencing, lease
heartbeats, retries, validation failures, OCR provenance/uncertainty, retrieval,
and preservation of the previous active index.

Local tests cover native and mixed scanned pages, OCR fallback and error handling,
review-blocked jobs, reviewed imports, legacy schema migration, page-label/index
separation, source-hash matching, and preservation of translations and
`is_correct: false`. Real Korean/English Tesseract was also exercised on a scanned
page from the supplied textbook. AWS/OpenAI live calls and container builds were
not run; Docker is unavailable in this local environment.

Reference APIs: [LangChain embeddings](https://reference.langchain.com/python/langchain-core/embeddings/embeddings/Embeddings/embed_documents),
[LangChain retrievers](https://reference.langchain.com/python/langchain-core/vectorstores/base),
and [S3 conditional object writes](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).

OCR references: [Tesseract CLI](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html), [PDFium rendering](https://pypdfium2.readthedocs.io/en/stable/python_api.html).

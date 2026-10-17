# Vocard administrator book ingestion

For a password-protected browser chat on Render Free using Gemini, see
[the Render deployment guide](deploy/render/README.md). The root `render.yaml`
deploys that chat separately from the ingestion services described below.

FastAPI accepts administrator-authorized uploads and records durable jobs. A separate
worker uses LangChain documents, text splitting, and OpenAI embeddings to build an
immutable index in private S3. A LangChain `BaseRetriever` resolves the active index
at query time. There is no administrator dashboard or in-request ingestion worker.

Fixed Q&A fixtures, held-out evaluation, model comparison, grading provenance and
measurement commands are in [evaluation/README.md](evaluation/README.md).

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
.venv/bin/vocard search grammar-book '이 문장의 동명사는?' --current-lesson 26 --top-k 4
.venv/bin/vocard lesson grammar-book --lesson-number 26
.venv/bin/vocard lesson grammar-book --title '가정법 과거완료'
```

The ingest command returns immediately after private uploads and the database
transaction, with `job_id`, `book_id`, PDF `sha256`, numeric `book_version`,
`extraction_status`, and queue status. `book` returns the active index version,
source version history, and immutable index history. Failed-job retry is explicit
and audited by administrator subject; re-uploading the same failed input returns
the same failed job rather than spending more embedding requests automatically.

## Grammar-point chunks and lesson retrieval

New indexes group text by reviewed grammar headings, carrying a continuing point
across PDF page boundaries. A point stays in one chunk when it fits. Larger points
split at explanation/example boundaries; a preceding explanation and its example
stay together when their combined text fits. Examples and translations that fit
within the size limit are kept intact. An oversized example is split only when
necessary and marked `example_split`. Source spans verify that every original
character remains covered. PDF page joins add an explicit newline; source wording
and incorrect examples are preserved.

Configure the character budget with `VOCARD_CHUNK_SIZE` or per ingestion:

```sh
.venv/bin/vocard ingest grammar-book /absolute/path/book.pdf \
  --course-id english-grammar --unit-id unit-1 --chunk-size 1200
```

`VOCARD_CHUNK_OVERLAP` is a maximum context allowance: overlap repeats whole atoms
when they fit, rather than cutting through an example to force a fixed overlap.
Course/unit defaults are snapshotted in the job. Reviewed pages can override them
with `course_id`, `unit_id`, `lesson_number`, and `lesson_title`. Missing course,
unit or lesson identifiers remain `null`; they are never invented. Clear
`Must 26` / `Lesson 26` headings supply lesson numbers and titles when available.
Ambiguous OCR such as `Must O1` does not become a number or inherit the last lesson.
Supply explicit lesson metadata in reviewed JSON for reliable exact title lookup.

Each new chunk stores `book_id`, `version` (source book version), `course_id`,
`unit_id`, `lesson_number`, `lesson_title`, one-based `source_pages`, and a stable
`chunk_id` equal to its LangChain document ID. It also stores `key_point_id`,
`key_point_title`, exact `source_spans`, and the associated structured grammar,
including translations and `is_correct`. Printed labels for all contributing
pages remain in `source_page_labels`. Returned documents additionally carry the
active `index_version` UUID and `retrieval_scope`.

`ActiveBookRetriever` is a LangChain `BaseRetriever`; use `invoke` / `ainvoke`.
Trusted application code must supply `authorized_book_ids` from verified account
authorization. Passing only a selected book ID is insufficient:

```python
from vocard.retrieval import ActiveBookRetriever

retriever = ActiveBookRetriever(
    repository=repository,
    storage=storage,
    book_id="grammar-book",
    authorized_book_ids=authorized_book_ids,  # Trusted authorization, not query content.
    current_lesson=26,
    course_id="english-grammar",
    unit_id="unit-1",
    top_k=4,
    max_fallback=2,
    min_score=0.0,
)
documents = retriever.invoke("이 문장의 동명사는?")
exact = retriever.lookup_lesson(lesson_number=26)
```

The retriever authorizes the selected book before reading storage, resolves the
active pointer for every call, and verifies index identity plus every row's book,
source version and source hash. Query text, custom metadata, and caller filters
cannot select another book or an inactive index. The API rejects book/version
filter overrides. If publication changes the active index during retrieval, it
returns HTTP 409 with no stale results; retry the request. Existing immutable
page-based indexes remain readable under the same book/version checks; re-ingest
with pipeline revision 3 to obtain grammar-point chunks and lesson metadata.

Lesson preference and fallback are deterministic:

- Rank relevant chunks from `current_lesson` (or `current_lesson_title`) first.
- Only fill remaining `top_k` slots from the same authorized book and active index,
  up to `max_fallback`. Both passes apply the same `min_score` threshold.
- `allow_book_fallback: false` / CLI `--no-book-fallback` disables the second pass.
  A missing or irrelevant lesson then returns no results. A zero fallback limit
  also disables it. Results identify `current_lesson`, `same_book_fallback` or `book`.
- Course/unit filters narrow both passes. Omit them to allow fallback throughout
  the book. With no current lesson, ordinary book retrieval uses `top_k` directly.

Defaults are `VOCARD_TOP_K=4`, `VOCARD_MAX_FALLBACK=2`, and `VOCARD_MIN_SCORE=0`.
`POST /admin/books/{book_id}/search` accepts `query`, `top_k` (`k` remains an alias),
`current_lesson`, `current_lesson_title`, `course_id`, `unit_id`,
`allow_book_fallback`, `max_fallback`, and `min_score`.

Exact lookup uses `GET /admin/books/{book_id}/lessons?lesson_number=26` or
`?title=...`, with optional course/unit filters. Matching normalizes Unicode,
whitespace and case; it never uses substring matching, embeddings or fallback.
When both number and title are supplied, both must match. Repeated lesson numbers
or titles across units can be disambiguated with `unit_id`. API results paginate
with `limit` (default 50), `offset`, `total_chunks`, and `next_offset`.

The signed administrator JWT may include `book_ids: ["grammar-book"]` to restrict
book access across ingestion, status, extraction, retrieval and exact lookup.
Omitting that claim preserves the existing global-administrator role. Retrieval
scope always comes from that verified principal; course/unit query parameters
only narrow results and do not grant access.

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
   active pointer. Retrieval detects a pointer change during a query and asks the
   caller to retry instead of returning the stale snapshot.

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

Retriever tests additionally cover cross-page explanation/example grouping, size
limits, stable chunk IDs, exact lookup, lesson-first ranking, bounded fallback,
scoped JWTs, corrupted row identities and active-version changes. An offline
rechunking check on the supplied 425-page OCR draft retained every source span:
439 detected grammar points, 628 chunks, 279 cross-page chunks, and a maximum of
1000 characters per chunk. Only 73 lesson numbers were unambiguous in that draft;
missing or ambiguous OCR metadata requires review before reliable exact lookup.

Reference APIs: [LangChain embeddings](https://reference.langchain.com/python/langchain-core/embeddings/embeddings/Embeddings/embed_documents),
[LangChain retrievers](https://reference.langchain.com/python/langchain-core/vectorstores/base),
and [S3 conditional object writes](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).

OCR references: [Tesseract CLI](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html), [PDFium rendering](https://pypdfium2.readthedocs.io/en/stable/python_api.html).

## Authenticated textbook answers

`POST /admin/books/{book_id}/answer` retrieves from the authorized book's active
index, then uses LangChain structured output to generate an answer. It accepts
the same lesson/course/unit filters, `top_k`, score threshold and bounded fallback
settings as search. Access comes from the verified administrator JWT, never from
the question. For example:

```sh
.venv/bin/vocard ask grammar-book '동명사의 목적어를 설명해 줘' \
  --current-lesson 7 --no-book-fallback --top-k 4
# Optional: --practice, --language en
```

Each request is independent. History, previous messages and conversation IDs are
rejected, including on the administrator answering route. The CLI has no history option.
The response always has `answer`, `status`, `citations`, `source_examples`, and
`practice_examples`. Status is `answered`, `insufficient_evidence`, or
`clarification_needed`; a clarification contains one focused question.

Each citation has a retrieved `chunk_id` and a verbatim supporting `quote`.
The server adds `book_id`, content `version`, `index_version`, 1-based
`source_pages`, separate `source_page_labels`, course, unit and lesson metadata.
The model cannot supply these locations. Only chunks actually included in the
generation context can be cited, even if another ID exists elsewhere in the same
index. Unknown IDs, changed quotes, forged location fields and changed source
examples fail closed with HTTP 502. A version promotion during generation returns
409 so the caller can retry against the new active index.

Explanations default to Korean; source English examples retain their exact text,
including incorrect markers. `source_examples` copies translations and
`is_correct` from extraction metadata. Newly generated examples require
`include_practice_examples: true` (`--practice`) and appear separately with the
server label **새로 만든 연습 예문 (교재 원문 아님)**. Render that label alongside
each practice example. The answer prose is not the entire response: clients must
also render the citations and example fields. For Korean responses, ungrounded
English sentences in prose are rejected rather than displayed without a label.

No retrieved evidence (including no full chunk fitting the context budget)
returns `insufficient_evidence` without calling the generation model. A generated
answer without citations also abstains. Untrusted PDF text, metadata, question
are serialized as data in one human message; a separate constant
system message defines the rules, and the model has no tools. Citation/quote validation proves provenance; semantic
support and resistance to prompt injection still depend on model behavior.

Configure `VOCARD_ANSWER_MODEL` (default `gpt-4.1-mini`),
`VOCARD_ANSWER_TIMEOUT` (60 seconds per attempt, one retry),
`VOCARD_ANSWER_MAX_TOKENS` (3000), and `VOCARD_ANSWER_CONTEXT_CHARS` (60000).
Full chunks are included within the serialized context budget; they are never
silently truncated. Generation requires `OPENAI_API_KEY`, only when evidence is
available. Provider failures return sanitized HTTP 503 errors. The response
schema is in `schema/answer-response.schema.json`. Structured generation uses
[LangChain's ChatOpenAI structured output](https://reference.langchain.com/python/langchain-openai/chat_models/base/ChatOpenAI/with_structured_output).

Tests use a deterministic structured generator without provider calls. They
cover forged/unretrieved IDs, quotes, metadata, source examples, abstention,
clarification, practice labels, question/PDF data boundaries, authorization,
generation failures, active-index races, and CLI-to-HTTP answering.

## Firebase lesson API for Flutter

The Flutter app in `../Vocard` uses `POST /ai/answer`, independent of administrator
ingestion/search routes. It sends a Firebase **ID token** as `Authorization:
Bearer ...`, plus `question` and optional `course_id`, `unit_id`,
and positive `lesson_number`. The server verifies the token and revocation with
[Firebase Admin SDK](https://firebase.google.com/docs/auth/admin/verify-id-tokens)
and obtains the UID from those verified claims. Anonymous sessions are denied;
client UIDs, book IDs, history, and retrieval overrides are rejected.

Configure the API process before enabling this entry point:

```sh
export VOCARD_FIREBASE_PROJECT_ID=vocard-6f1a5
# Use your actual reviewed, published book ID:
export VOCARD_COURSE_BOOKS='{"devils_100":"devils-100"}'
# Supply Application Default Credentials for Firebase Admin on the server.
```

All non-anonymous users signed into that Firebase project can access courses in
this explicit server mapping. An empty mapping denies all student course access.
The app never chooses a book directly. Index metadata must match `devils_100`,
the asset unit ID such as `devils-grammar-unit-01`, and the explicit asset lesson
number (not the unit number or part position). Ingest matching reviewed JSON first;
uploading a book does not authorize or publish it automatically. Use a TLS endpoint;
Flutter web deployments need a same-origin gateway or an explicit gateway CORS
allowlist. No Firebase or cloud resources are deployed by this change.

The response contains `answer`, `status`, `citations`, `source_examples`, and
`practice_examples`; it has no conversation ID. `GET /ai/conversations` and the
conversation-by-ID endpoint no longer exist. Unknown fields, including
`conversation_id`, `history`, `messages`, and `previous_messages`, receive 422
without echoing the rejected input.

If the course is omitted, a single unique book in the server course mapping can
be selected. Multiple books require a focused course clarification; an empty map
denies access. Optional unit/lesson filters refine retrieval within that book.
Every scoped question invokes retrieval against the current active version, using
only that question. Previously displayed answers never enter retrieval or the
model prompt. Context-dependent questions such as “왜 그런가요?” ask for the missing
sentence/rule. The model also receives instructions to clarify missing references.

`VOCARD_AI_DAILY_QUOTA` defaults to 50 admitted questions per verified UID per UTC
day. Atomic reservations survive API restarts and count failed generation attempts
and clarification requests. Only `(uid, day, count)` is stored for this feature.
Firebase authentication, private PDFs/book indexes and resolved citations remain.
There are no conversation leases or content persistence. The Flutter client keeps
at most ten displayed exchanges in screen memory, clears them on account change
and disposal, and sends only the current question plus optional lesson context.
Opening the screen performs no history request. Offline/timeout/authentication/
quota errors preserve the draft in that screen for retry. Each retry is independent.

See [application privacy and provider retention](docs/ai-privacy.md) for the
content-free telemetry policy, deployment controls and legacy data migration.
Tests cover independent requests, fresh active versions, forbidden history fields,
optional context, authentication, atomic UID quotas, database content absence,
tracing/cache isolation, sanitized errors and Flutter disposal.

## Standalone local playground

Run local AI chat with Ollama, without API keys, S3, or administrator JWTs:

```sh
# Install Ollama from https://ollama.com if needed, then start it.
ollama pull qwen2.5:3b
# Optional: set VOCARD_CHAT_MODEL to another installed chat model
.venv/bin/uvicorn vocard.chat:create_chat_app --factory --host 127.0.0.1 --port 8001
```

Open http://127.0.0.1:8001. Enter sends; Shift+Enter adds a newline.
New chat or refreshing clears displayed messages. Each request sends only the
current question, with bounded input and output. Missing references require clarification. This is general AI Q&A, not textbook
retrieval. The chat endpoint calls only local Ollama at 127.0.0.1:11434; no
OpenAI key is used and there are no API fees. Keep this unauthenticated test app
bound to loopback, separate from the authenticated ingestion API.

The playground now includes a grammar sidebar with the textbook's 13 chapters
plus its introduction and contents. Selecting a part displays all of its PDF-page
text and opens a separate chat for that part. Chats and drafts remain separate
while switching parts, and clear on refresh. Chat receives bounded excerpts from
the selected part as reference; this does not use the production vector index.
The source remains explicitly labeled as an unreviewed OCR draft.
Set `VOCARD_GRAMMAR_PATH` to use a different extraction JSON; the default is
`output/pdf/textbook-extraction/textbook.json` relative to the working directory.

The grammar playground has a left sidebar of textbook parts. Selecting a part
opens its OCR text and a chat grounded in excerpts from that part. Conversations
and drafts stay separate per part for the current browser session. Source text
comes from `output/pdf/textbook-extraction/textbook.json`; set
`VOCARD_GRAMMAR_PATH` to use another extraction file. The OCR draft is unreviewed.

## Vocabulary tutoring

The vocabulary study **Explain this word** action uses the Firebase-authenticated
`POST /ai/vocabulary/explain` endpoint. It reuses Vocard's selected `VocabularyWord`
entry, course ID, meanings and paired examples/translations. The tutor explains
the sentence-specific meaning first, then nuance and bilingual examples, with
explicit labels distinguishing app-supplied Vocard data, retrieved book passages
and AI-generated content. Ambiguous senses produce alternatives or a focused
clarification. Correct/incorrect source markers are preserved and validated.

Vocabulary tutoring works without a mapped book; an explicit course-to-book map
adds fresh, authorized active-index evidence. Follow-ups send only bounded,
untrusted context from Flutter screen memory. Closing the screen discards it;
there is no server conversation or content persistence. The grammar answering
contract remains independent and evidence-only. See the complete
[vocabulary API contract](docs/vocabulary-tutor-api.md), including request limits,
source labels, retrieval authorization, errors and privacy behavior.

## API reference documents

The maintained API reference is [docs/api-reference.md](docs/api-reference.md);
its shareable PDF is [output/pdf/vocard-api-documentation.pdf](output/pdf/vocard-api-documentation.pdf).
Whenever an API contract changes, update this reference, the relevant focused
contract/schema, and the PDF in the same change, as required by `vocard/AGENTS.md`.

Check route coverage using the project environment:

```sh
.venv/bin/python tools/build_api_documentation.py --check
```

Regenerate the PDF in a documentation Python environment with `reportlab` and a
Korean-capable TrueType font (these are not API runtime dependencies):

```sh
python tools/build_api_documentation.py --font /path/to/KoreanFont.ttf
```

On macOS the builder automatically finds AppleGothic; on Linux it checks for
NanumGothic. Render and inspect the generated PDF before including it in a change.

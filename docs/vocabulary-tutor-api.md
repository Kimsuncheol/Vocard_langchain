# Vocabulary tutoring API

`POST /ai/vocabulary/explain` extends the Firebase student API. Grammar Q&A remains
`POST /ai/answer` with its existing evidence-only contract. Vocabulary explanations
may use general language knowledge, explicitly labeled as AI generated.

## Authentication and request

Send `Authorization: Bearer <Firebase ID token>`, `Content-Type: application/json`
and `Cache-Control: no-store`. Firebase verification/revocation checks and the
shared `VOCARD_AI_DAILY_QUOTA` apply. Anonymous users are denied. Only the existing
Vocard course IDs are accepted: `suneung`, `toeic`, `toefl`, `ielts`, `gtelp`,
`idioms`, `suneung_idioms`, and `collocations`.

```json
{
  "question": "이 문장에서 bank는 어떤 뜻인가요?",
  "selected_entry": {
    "id": "ielts-bank",
    "courseId": "ielts",
    "word": "bank",
    "meaning": "(n) 은행; 강둑",
    "explanation": "문맥에 따라 뜻이 달라집니다.",
    "exampleSentences": ["She sat on the bank."],
    "exampleTranslations": ["그녀는 강둑에 앉았다."]
  },
  "sentence": "She sat on the bank of the river.",
  "prior_explanation": null
}
```

The selected entry reuses `VocabularyWord`'s camelCase field names, paired example
arrays and visible translations (`{{meaning}}` display marks are removed by its
existing translation getter). The DTO omits study ordering/progress fields.
`id`, `courseId`, `word`, and `meaning` are required; explanation/example arrays
may be omitted. Question: 1–4000 characters. Sentence and temporary prior explanation:
optional, 1–2000 characters each. At most 20 examples and 20 translations; each
is at most 2000 characters. Total serialized generation context is bounded by
`VOCARD_ANSWER_CONTEXT_CHARS`. Unknown fields, conversation IDs, message arrays,
book IDs and retrieval overrides are rejected. Empty strings should be omitted
for optional fields.

All client values are untrusted data, including the asserted entry ID/course,
meanings, sentence, translations and prior explanation. The API does not claim
the entry has been verified against a server-side vocabulary catalog. These
values cannot modify system instructions, select an arbitrary book or supply
trusted citations.

## Response and sources

The response contains:

- `status`: `answered`, `ambiguous`, or `clarification_needed`.
- `contextual_meaning`: `{part_of_speech, explanation_ko, source_ids}`. The meaning
  fitting the supplied sentence comes first; null when clarification is required.
- `interpretations`: alternative contextual meanings when ambiguous (at least two).
- `nuance`: `{register, connotation, collocations, comparisons, source_ids}`, or null
  for clarification. Explanations are concise Korean; English expressions remain intact.
- `examples`: `{english, translation, source_id, is_correct, label}`. Translations
  are Korean; a verbatim source example without a supplied translation can use null.
- `suggested_follow_up_questions`: 1–4 suggestions, each at most 400 characters.
- `clarification_question`: one focused Korean question or null.
- `sources`: server-created `{source_id, kind, label, citation}` records.

`model` has kind `model_generated`, labeled **AI 생성 설명 · 교재 인용 아님**.
Every meaning/nuance explanation includes this label, even when it also draws on
Vocard or book data. Generated examples carry **AI 생성 예문 (교재 원문 아님)**.
They must be marked correct and include a Korean translation.

`vocard:entry` and `vocard:example:N` have kind `vocard_data`, labeled as
**앱 제공 · 서버 미검증**. Example numbers are zero-based indexes into the selected
entry. Referenced source example text/translation pairs must match exactly.
Correct/incorrect markers remain in the text; a recognized incorrect marker
cannot become `is_correct=true`. Incorrect examples display a warning, never
an affirmative correct-example badge. A model may flag an unmarked source example
as incorrect; unverified client data is not proof of grammatical correctness.

`book:<chunk_id>` has kind `book_passage`. Its `citation` contains the retrieved
chunk ID, exact supporting quote, server-resolved book/version/index version,
1-based PDF pages, separate printed page labels, and original course/unit/lesson
metadata. Each referenced book source must have a valid citation; unused or
unknown citations and changed quotes/examples fail closed. Book examples must
match the retrieved extraction, including translations and `is_correct` markers.
The model is instructed to cite passages only when they support the explanation.
Verbatim/ID validation establishes provenance; it does not independently prove
semantic entailment or the accuracy of generated language guidance.

No `conversation_id` is returned. A clarification response has no meaning,
nuance, interpretations, examples or book citations; it asks one focused question.
An ambiguous response shows likely interpretations and may ask for more context.

## Authorized book retrieval

An explicit `VOCARD_COURSE_BOOKS` mapping may assign a reference book to a vocabulary
course. Each request retrieves again from only that book's active index. The
query contains the word, current sentence and current question, never the prior
explanation. The mapped book can have its own original course metadata: for this
supplementary vocabulary lookup, the authorized book ID is the boundary, rather
than requiring its pages to share the vocabulary course's ID. The grammar route's
course/unit/lesson filtering is unchanged. Index changes during generation return
409 and require a fresh request.

Without a mapped book, vocabulary tutoring still works with Vocard data and
clearly labeled model knowledge; no book passages can be cited. There is no
fallback to another configured book. A mapped book with no active index returns
503 instead of pretending retrieval succeeded.

## Temporary follow-up context and privacy

Flutter holds the selected entry, editable sentence, latest response and a
maximum 2000-character excerpt of the latest relevant explanation in screen
memory only. Suggested and typed follow-ups send the selected entry, current
sentence, current question and `prior_explanation` again. No complete exchanges,
previous questions, citation objects or conversation IDs are replayed. Changing
the sentence clears the response/excerpt. Closing the screen or changing accounts
clears temporary state; late results are discarded. Opening the screen performs
no restore request. Vocabulary remains visible offline; AI answers require a
connection and are never loaded from a stored answer cache. Failed requests keep
only the current in-screen draft for an explicit retry.

The backend persists only usage metadata `(uid, UTC day, count)`. Both tutoring
and grammar requests share that quota, including admitted failed calls. Questions,
answers and temporary context are not written to databases, files, application
caches, logs, analytics or traces. The same no-store response headers, sanitized
validation/provider errors, tracing isolation, disabled SDK/access logs, and
`ChatOpenAI(cache=False, store=False)` apply. Existing private books/indexes remain.
See [AI privacy and provider retention](ai-privacy.md) for deployment controls and
legacy-data cleanup. The prior excerpt is sent to the generation provider; it is
not sent to query embedding. Application statelessness does not establish zero
retention by the provider.

## Errors and verification

401/403: authentication or unknown vocabulary course; 422: invalid/oversized
context; 429: daily quota; 409: active-index change; 502: schema/provenance/marker
validation failed; 503: provider/retrieval unavailable. Errors never echo question
or provider content. The Flutter client retains existing offline, timeout,
authentication, quota and network failure handling and refreshes a rejected token
once. No automatic generation retries occur at the UI layer.

Machine-readable contracts are in `schema/vocabulary-question.schema.json` and
`schema/vocabulary-tutor-response.schema.json`. Deterministic mocked-provider tests
exercise contextual-sense fixtures, ambiguity, untrusted context, source/marker
validation, active versions, authentication/quota and absence of content in SQLite
or collected logs/traces. Flutter tests cover entry serialization, source labels,
suggested/free-text follow-ups, bounds, sentence reset, disposal and offline/error
states. These tests verify integration and validation, not live-provider linguistic
accuracy. No live Firebase/OpenAI calls are used in these tests.

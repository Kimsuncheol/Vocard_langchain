# Render Free + Ollama Cloud

The root `render.yaml` runs the lightweight browser textbook chat on Render Free.
Inference runs in Ollama Cloud; no Ollama installation, model downloads, S3,
SQLite, worker, or persistent disk is needed on Render. The tracked textbook
JSON ships with the source. Chats clear on refresh.

1. Create an Ollama API key at https://ollama.com/settings/keys.
2. Choose an available cloud model from https://ollama.com/search?c=cloud.
   Use its direct API model name from `https://ollama.com/api/tags`;
   local model names such as `qwen2.5:3b` are not automatically cloud models.
3. Push the changes to your repository and create a Render Blueprint using
   `render.yaml`, with the Free service plan. For an existing service, update
   its environment settings to match the Blueprint.
4. Set `OLLAMA_API_KEY` and `VOCARD_CHAT_MODEL` when prompted.
   `VOCARD_CHAT_PROVIDER=ollama` and
   `VOCARD_OLLAMA_BASE_URL=https://ollama.com` are configured by the Blueprint.
   Remove the old `GEMINI_API_KEY` if switching an existing service.
5. Retrieve the generated `VOCARD_CHAT_PASSWORD` from Render's environment
   settings and sign in at the service HTTPS URL as `vocard`.
6. Verify `/healthz` returns 200, unauthenticated `/` returns 401, and an
   authenticated textbook question returns an answer. Test from mobile data.

The Ollama base URL is the server origin, without `/api/chat`. The server sends
`Authorization: Bearer <OLLAMA_API_KEY>` to it. Keep keys in Render environment
settings; never put them in browser code or source control. Hosted Ollama
requires HTTPS, a provider key, and a chat username/password (at least 16
characters for the password). A custom Ollama host must expose an authenticated
HTTPS gateway accepting this Bearer token. Never expose a bare Ollama port.

Local development defaults to `http://127.0.0.1:11434`, `qwen2.5:3b`, and no
provider key. On Render, loopback refers to the Render container, not your Mac.
For local inference on your Mac with a Render frontend, configure an authenticated
HTTPS tunnel origin and its token instead; the Mac must stay awake and online.

Render Free sleeps after 15 idle minutes and can take about a minute to wake.
It provides 512 MB RAM and no persistent disk, so do not run model inference
there. Free instance hours, bandwidth, and build limits still apply. Ollama
Cloud has separate plan limits and pricing; check your account before use.
Questions and selected textbook excerpts are sent to the configured provider.
This app uses shared password access, not individual accounts.

Provider calls have a 180-second timeout. Quota exhaustion returns 429 without
automatic retries or a paid-provider fallback. The health check makes no AI call
and does not verify provider credentials or model access. Gemini remains
available through the previous provider environment variables if needed.

References:
- https://docs.ollama.com/cloud
- https://ollama.com/pricing
- https://render.com/docs/free
- https://render.com/docs/compute-plans

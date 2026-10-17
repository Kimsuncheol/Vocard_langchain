# Render Free + Gemini

Deploy the browser textbook chat using the root `render.yaml` Blueprint.
No Ollama, S3, SQLite, worker or disk is required. The tracked textbook JSON
is read from the deployed source. Chats clear on refresh as before.

1. Create a Gemini API key at https://aistudio.google.com/apikey using a project
   on the free tier. Leave billing disabled to avoid paid usage. Confirm model
   access and quotas in AI Studio; availability and allowances vary.
2. Push these changes to your GitHub repository. In https://dashboard.render.com,
   choose New > Blueprint and connect the repository. Select the branch containing
   `render.yaml`. Confirm the service plan is Free.
3. Supply `GEMINI_API_KEY` when prompted. Keep it in Render's environment settings,
   never in the repository, browser code, or chat messages.
4. After deployment, retrieve the generated `VOCARD_CHAT_PASSWORD` from the service
   environment settings. Open its HTTPS `.onrender.com` URL and log in as `vocard`.
5. Confirm `/healthz` returns 200, opening `/` without credentials returns 401,
   and authenticated textbook navigation and a Gemini answer work. Test from your
   phone using mobile data. Confirm a bad password cannot reach `/api/chat`.

The app refuses to start in hosted mode without authentication and a Gemini key.
Gemini quota exhaustion returns 429, without automatic paid fallback or retries.
Render Free sleeps after 15 idle minutes, so the next visit can be slow.
The health endpoint does not make paid or quota-consuming AI calls.

Gemini free-tier inputs/outputs may be used to improve Google's products;
questions and selected textbook excerpts are sent to Google. This deployment
uses shared password access, not individual user accounts.

References:
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/rate-limits
- https://render.com/docs/free

# Deploying DataWeave

DataWeave is a **Python FastAPI** app that serves both the API and the bundled
React UI from one process, and streams responses over **SSE**. It needs a host
that can run a long-lived ASGI server (`uvicorn`) without buffering.

> **Not suitable:** CloudLinux / cPanel / Plesk *shared* hosting. Those run
> Python only through Passenger (which buffers and breaks SSE streaming) and
> restrict long-running processes. Use a container host instead.

The repo ships a `Dockerfile`, so any container host works. Below is Render
(free tier); Railway is identical (it auto-detects the `Dockerfile`).

## Deploy on Render (from GitHub, auto-deploy)

1. Push this repo to GitHub (already done — `main`).
2. Render → **New → Blueprint** → connect the repo. Render reads `render.yaml`.
3. Set the secret env vars in the dashboard (values from your local `.env`):

   | Variable | Purpose |
   |---|---|
   | `GEMINI_API_KEY` | Gemini LLM/vision |
   | `GROQ_API_KEY` | Groq LLM |
   | `NVIDIA_NIM_API_KEY` | NVIDIA NIM (vision/LLM) |
   | `JINA_API_KEY` | Embeddings + reranker |
   | `OCR_SPACE_API_KEY` | OCR fallback |
   | `OPENROUTER_API_KEY` | OpenRouter (default provider) |
   | `OPENROUTER_TEXT_MODEL` | e.g. `tencent/hy3:free` |
   | `OPENROUTER_VISION_MODEL` | OpenRouter vision model |
   | `QDRANT_URL` | Qdrant Cloud endpoint |
   | `QDRANT_API_KEY` | Qdrant Cloud key |

   `DEFAULT_PROVIDER` and `DB_ENGINE` are preset in the blueprint.

4. Deploy. When it's live, open the Render URL — the chat UI loads and
   `/(api/health)` shows which providers are configured.

Every push to `main` redeploys automatically (`autoDeploy: true`).

## Notes

- **Streaming works out of the box** — uvicorn serves SSE directly, no proxy
  buffering to fight.
- **Qdrant is external** (Qdrant Cloud), so vectors persist across redeploys.
- **Document metadata is durable and host-independent.** The ingestion
  registry (document identity, versions, active/superseded state, dedup
  hashes) lives in Qdrant alongside the embeddings — a single source of truth,
  no persistent local disk required. A redeploy, container restart, or move to
  any other host (Docker, K8s, VPS, any PaaS) comes back with the correct
  state as long as `QDRANT_URL`/`QDRANT_API_KEY` point at the same cluster.
  When Qdrant isn't configured, the registry falls back to a local JSON file
  for development only. Existing installations migrate their old
  `data/ingested_files.json` into Qdrant automatically on first startup.
- **Chat history is still local** (`data/`), which resets on redeploy/sleep on
  the free tier. Uploaded files under `data/uploads/` are transient too — their
  content already lives in Qdrant. For durable chat history, attach a
  persistent disk (paid) mounted at `/app/data`, or move it to a DB later.
- **Cold starts:** the free plan sleeps after ~15 min idle; the first request
  after that takes ~30s to wake.

## Run locally with Docker

```bash
docker build -t dataweave .
docker run --rm -p 8000:8000 --env-file .env dataweave
# open http://localhost:8000
```

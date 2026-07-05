# transub

Cloud-based subtitle (SRT) translator: English → Indonesian. Exposes a single
HTTP endpoint that accepts a base64-encoded SRT and returns a base64-encoded
translated SRT.

The translation pipeline is **API-driven** (OpenAI-compatible chat completions),
not a local model. The repo previously hosted a vLLM/RP handler (branches
`vllm`/`main`); the current `cloud` branch is FastAPI-only.

## Architecture

```
┌──────────┐    POST /translate    ┌────────┐    chat.completions    ┌──────────┐
│  client  │ ────────────────────▶ │ app.py │ ────────────────────▶ │  cloud   │
│  (curl)  │   {"srt_text_base64"} │ FastAPI│   threaded pool       │ OpenAI-  │
│          │ ◀──────────────────── │        │ ◀──────────────────── │ compatible│
└──────────┘   {"translated_srt_   └────────┘   batched chunks      │ endpoint │
                 base64"}           translate.py                    └──────────┘
```

- `app.py` — FastAPI entrypoint. Defines `/health` and `POST /translate`.
  Uses `run_in_threadpool` to keep the event loop free while
  `translate_srt()` does blocking HTTP work.
- `translate.py` — the entire translation engine. Parses SRT, groups cues
  into sentences, batch-translates via the chat API, reconstructs
  per-cue lines, re-indexes, returns SRT.
- `test.py` — local CLI: encodes `test/sub.srt` to base64, calls
  `translate.handler()` directly, writes a timestamped output SRT.
- `Dockerfile` + `docker-compose.yml` — single-stage image
  (`python:3.10-slim`), image tag `urisuzy/transub:latest`, joins the
  external `transub-net` Docker network.

## Translation pipeline (`translate.translate_srt`)

1. **Parse** SRT with the `srt` library.
2. **Strip** hearing-impaired markup (`(...)`, `[...]`) and drop empty cues.
3. **Group cues → sentences** (`group_into_sentences`): subtitle files
   often split one sentence across multiple cues. We accumulate cues
   until we hit a sentence-ending punctuation (`.!?…` plus optional
   closing quotes/brackets), so the model sees a full sentence.
4. **Batch into chunks** of `CHUNK_SIZE` sentences (default 100).
5. **Pass 1 — Scan glossary** (sequential, cheap). For each chunk we
   send a small request asking the model to return JSON of recurring
   proper nouns / nicknames / special terms with a suggested Indonesian
   rendering (`{EN: ID}`). Sequential because the scan is small and
   "first writer wins" — the first chunk to see a term locks its
   translation. Chunks that return malformed/empty JSON contribute
   nothing (defensive: pipeline still works, just loses consistency
   for that chunk's terms).
6. **Pass 2 — Translate in parallel** via
   `ThreadPoolExecutor(CONCURRENCY)`. Each chunk is sent as a numbered
   list ("1. ... 2. ... 3. ...") with strict instructions to keep the
   numbering and not merge/split lines. The locked glossary from
   Pass 1 is injected as a `ISTILAH TETAP` block in every chunk's
   prompt.
7. **Post-process enforce**: after the model returns each translated
   chunk, `postprocess()` runs a case-insensitive whole-word regex
   replace over every `EN → ID` pair in the glossary. Safety net for
   cases where the model ignored the prompt block.
8. **Reconstruct per-cue** (`distribute_translation`): split the returned
   translation back into the original cue count. Three-tier fallback:
   - **Tier 1** — match the trailing punctuation of the source cue in
     the target text (handles >95% of cases).
   - **Tier 2** — character-ratio target + punctuation anchor window
     (25% of total length).
   - **Tier 3** — nearest word boundary inside the window.
9. **Re-index** cues 1..N and `srt.compose()` back to text.

**Adaptive retry**: if a chunk's numbered output is missing/blank lines,
the chunk is split in half and retried recursively. This protects
large-chunk economics — per-sentence fallback only fires when a chunk
of size 1 still misaligns. The glossary flows through the recursive
split too.

## Endpoints

| Method | Path        | Body                                  | Response                                  |
|--------|-------------|---------------------------------------|-------------------------------------------|
| GET    | `/health`   | —                                     | `{"status": "ok"}`                        |
| POST   | `/translate`| `{"srt_text_base64": "<b64>"}`        | `{"translated_srt_base64": "<b64>"}`      |

400 on missing/empty payload or undecodable base64. 500 on translation
failure (after `MAX_RETRIES + 1` attempts).

## Environment variables (all read in `translate.py`)

| Var | Default | Purpose |
|-----|---------|---------|
| `OPENAI_BASE_URL` | `http://157.180.30.121:20128/v1` | OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | `""` | **Required.** The `client` falls back to `"EMPTY"` if unset, which will 401. |
| `MODEL` | `openrouter/deepseek/deepseek-v4-flash` | Model name passed to chat completions |
| `CHUNK_SIZE` | `100` | Sentences per batched request |
| `CONCURRENCY` | `8` | Parallel chat-completion workers |
| `TEMPERATURE` | `0.3` | Sampling temperature |
| `MAX_RETRIES` | `2` | Per-request retries (3 total tries) |
| `MAX_TOKENS` | `8192` | Output cap per request — must be high enough for `CHUNK_SIZE × ~60 tokens` |
| `MAX_TOKENS_SCAN` | `1024` | Output cap for Pass 1 (glossary scan). Output is a small JSON object, so 1024 is plenty. |
| `DISABLE_THINKING` | `1` | When truthy, sends `extra_body={"reasoning": {"enabled": False}}` to suppress model reasoning tokens. **Biggest cost lever** — reasoning tokens dominate output cost. Set to `0`/`false`/`no`/empty to re-enable. |
| `PORT` | `8000` | Host port (compose only) |

`replacements = []` in `translate.py` is a placeholder post-processing
list (e.g. swap "Anda" → "Kau"); currently a no-op.

## Commands (`Makefile`)

```bash
make up       # docker compose up -d --build
make down     # docker compose down
make logs     # docker compose logs -f
make build    # docker compose build
make push MSG="..."   # git add -A && commit -m "..." && push
```

Local test (no Docker):
```bash
python3 test.py     # reads test/sub.srt, writes test/sub_translated_<ts>.srt
```

## Deployment

- Image: `urisuzy/transub:latest`, built from this Dockerfile.
- Joins the **external** Docker network `transub-net` so other containers
  on the same host can call it as `http://transub:8000/translate`. The
  network must exist on the host first:
  `docker network create transub-net`
- Port: `${PORT:-8000}` → container 8000 (uvicorn).
- `restart: always` — survives host reboots.

## Conventions & gotchas

- **Cloud-only**: this branch has no local model. The `vllm` branch is
  the previous self-hosted path. Don't try to load a model from this
  repo; translation only works when `OPENAI_BASE_URL` and
  `OPENAI_API_KEY` point to a reachable endpoint.
- **DISABLE_THINKING is the cost lever**: the default model
  (`deepseek-v4-flash`) emits a long reasoning preamble. Without
  `DISABLE_THINKING=1`, output tokens roughly double. Leave it on
  unless debugging the model itself.
- **Pass 1 scan failures are non-fatal**: the glossary scan occasionally
  returns empty or malformed JSON for a chunk. The pipeline continues
  with whatever glossary it has so far. The `DISABLE_THINKING=1` flag
  helps reduce empty responses from reasoning-mode models.
- **MAX_TOKENS must scale with CHUNK_SIZE**: each translated line is
  ~30–60 tokens. With `CHUNK_SIZE=100` you need at least ~6000
  output tokens of headroom; 8192 is the safe default. If you raise
  `CHUNK_SIZE`, raise `MAX_TOKENS` too, or chunks will truncate and
  fall back to single-sentence mode.
- **Test outputs are git-ignored**: `test/sub_translated_*.srt` is
  in `.gitignore` — these are timestamped runs, not source.
- **.env is git-ignored** but present in this working copy — contains
  a real `OPENAI_API_KEY`. Don't commit it; `cp .env.example .env` is
  the bootstrap path.

## File map

```
app.py              FastAPI app: /health, POST /translate
translate.py        Translation engine (parse → group → batch → reconstruct)
test.py             Local CLI test harness
Dockerfile          python:3.10-slim, uvicorn on :8000
docker-compose.yml  transub service, transub-net external network
requirements.txt    openai, srt, fastapi, uvicorn[standard]
Makefile            up / down / logs / build / push
.env.example        Configurable template (copy to .env)
test/sub.srt        Sample input (English)
test/sub_translated*.srt  Sample outputs (git-ignored)
```

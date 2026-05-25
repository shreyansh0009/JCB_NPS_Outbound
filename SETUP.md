ve# Priya AI Voice Agent — Setup Guide

**Godrej Appliances Customer Support | Enterprise AI Voice Agent**
Scale: 50K–2L calls/day | 5K concurrent | AudioSocket (Asterisk) + Groq + Deepgram + ElevenLabs/Cartesia

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone & Install](#2-clone--install)
3. [Environment Configuration (.env)](#3-environment-configuration-env)
4. [Config Files to Check Before Testing](#4-config-files-to-check-before-testing)
5. [Prompts — Agent Behaviour](#5-prompts--agent-behaviour)
6. [Running the Server](#6-running-the-server)
7. [Verifying the Startup Log](#7-verifying-the-startup-log)
8. [Asterisk / AudioSocket Integration](#8-asterisk--audiosocket-integration)
9. [Admin & Ops Endpoints](#9-admin--ops-endpoints)
10. [Optional Enhancements](#10-optional-enhancements)
11. [Architecture Quick Reference](#11-architecture-quick-reference)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ (3.14 tested) | Use pyenv or system Python |
| pip | latest | `pip install --upgrade pip` |
| Asterisk | 18+ | With `app_audiosocket` module loaded |
| Redis | 7+ | Optional — only for multi-worker production |

**API Keys required before starting:**

| Service | Purpose | Get it at |
|---|---|---|
| Groq | LLM (llama-3.3-70b) | console.groq.com |
| Deepgram | Speech-to-Text (Nova-3) | console.deepgram.com |
| ElevenLabs | Text-to-Speech (Flash v2.5) | elevenlabs.io |
| Cartesia | TTS fallback (Sonic-3) | cartesia.ai |

---

## 2. Clone & Install

```bash
# Clone the repo
git clone <repo-url>
cd python_aivoice_agent

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Linux/Mac
# .venv\Scripts\activate           # Windows

# Install all dependencies
pip install -r requirements.txt

# Optional: neural VAD for better barge-in detection (~200MB torch download)
pip install silero-vad
```

---

## 3. Environment Configuration (.env)

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

### 3.1 Mandatory — Must Set Before First Run

Open `.env` and set these. The server **will not work** without them:

```env
# ── LLM ──────────────────────────────────────────────────────────────────────
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GROQ_MODEL=llama-3.3-70b-versatile      # 70b recommended; 8b is faster but weaker

# ── STT ──────────────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY=your_deepgram_api_key_here

# ── TTS Primary ───────────────────────────────────────────────────────────────
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=your_elevenlabs_api_key_here
ELEVENLABS_VOICE_ID=<voice_id>          # Find at elevenlabs.io/voice-library
ELEVENLABS_MODEL=eleven_flash_v2_5      # Flash v2.5 supports all Indian languages

# ── TTS Fallback (auto-failover when ElevenLabs circuit opens) ────────────────
CARTESIA_API_KEY=your_cartesia_api_key_here
CARTESIA_VOICE_ID=<voice_id>            # Aarti or any Indian female voice
```

### 3.2 Telephony Ports

```env
# Asterisk connects here via AudioSocket protocol
AUDIOSOCKET_PORT=9093       # must match your Asterisk dialplan

# HTTP server for admin API, metrics, ops dashboard
PORT=9000                   # python main.py --port 9000
```

### 3.3 Important Optional Settings

```env
# ── Language / STT ────────────────────────────────────────────────────────────
DEEPGRAM_LANGUAGE=              # Leave EMPTY for auto-detect (recommended for Hindi+English)
                                # Set "hi" or "en-IN" to force a single language
DEEPGRAM_ENDPOINTING_MS=500    # Silence (ms) before STT finalises transcript
                                # 300 = faster response | 500 = absorbs natural pauses

# ── Redis (only needed for multi-worker production) ───────────────────────────
REDIS_ENABLED=false             # true = sessions shared across workers
REDIS_URL=redis://localhost:6379/0

# ── MCP / DMS (appointment booking) ──────────────────────────────────────────
DMS_API_KEY=your_dms_api_key    # Required only if DMS MCP server is configured

# ── Admin API Security ────────────────────────────────────────────────────────
ADMIN_API_KEY=                  # Empty = no auth (dev). Set strong key for production.

# ── Welcome Message (spoken when call connects) ───────────────────────────────
WELCOME_MESSAGE=नमस्ते! Godrej Support में आपका स्वागत है। मैं Priya हूँ।

# ── Per-language Voice Overrides (optional) ──────────────────────────────────
# Flash v2.5 works multilingually on any single voice.
# Set these only if you want a different voice per language.
ELEVENLABS_VOICE_HI=<hindi_voice_id>
ELEVENLABS_VOICE_MR=<marathi_voice_id>
```

---

## 4. Config Files to Check Before Testing

These JSON files control routing, MCP tools, tenants, and A/B tests. Review each before the first test call.

---

### 4.1 `config/squads/appliances_squad.json` — Agent Routing (CRITICAL)

Defines which agents exist and the handoff graph between them.

```json
{
  "name": "Godrej Appliances — Customer Support Squad",
  "leader": "hello",
  "agents": [
    { "name": "hello",     "can_handoff_to": ["screener"] },
    { "name": "screener",  "can_handoff_to": ["service", "sales", "scheduler", "closer"] },
    { "name": "service",   "can_handoff_to": ["scheduler", "closer", "sales"],
      "rag_collection": "godrej_appliances_service",
      "mcp_server": "dms" },
    { "name": "sales",     "can_handoff_to": ["service", "closer"],
      "rag_collection": "godrej_appliances_products",
      "mcp_server": "dms" },
    { "name": "scheduler", "can_handoff_to": ["closer"],
      "mcp_server": "dms" },
    { "name": "closer",    "can_handoff_to": [] }
  ]
}
```

**What to check:**
- `SQUAD_PATH` env var must point here (default is already correct)
- `leader` is always `hello` — the first agent to handle every call
- `rag_collection` names must match collections in your ChromaDB

---

### 4.2 `config/mcp_servers.json` — MCP Tool Server (CRITICAL for Scheduler/Service)

Controls appointment booking (`get_service_slots`, `book_service_appointment`) and inventory tools.

```json
{
  "servers": [
    {
      "name": "dms",
      "url": "https://your-dms-api.com/mcp",   ← CHANGE THIS
      "api_key_env": "DMS_API_KEY",
      "timeout": 10
    }
  ]
}
```

**Choose one option:**

| Your situation | Action |
|---|---|
| You have a real DMS API | Replace URL with your endpoint. Set `DMS_API_KEY` in `.env` |
| Local dev / testing only | Set `"servers": []` — MCP disabled, agents work without booking tools |
| Run a local mock server | `uvicorn mock_dms_server:app --port 8081`, set URL to `http://localhost:8081/mcp` |

**Warning:** If left as `https://your-dms-api.com/mcp`, all appointment booking attempts will throw connection errors at runtime.

---

### 4.3 `config/tenants.json` — Multi-Tenant DID Routing

Maps inbound phone numbers (DIDs) to tenant-specific configurations.

```json
{
  "default": {
    "tenant_id": "godrej_default",
    "name": "Godrej Appliances Support",
    "squad_path": "config/squads/appliances_squad.json",
    "welcome_message": "नमस्ते! Godrej Support में आपका स्वागत है।",
    "language_default": "hi"
  },
  "dids": {
    "+911800419000": {
      "tenant_id": "godrej_premium",
      "welcome_message": "नमस्ते! Godrej Premium Support में आपका स्वागत है।"
    }
  }
}
```

**What to check:**
- `default.squad_path` must match your squad file
- Add your real inbound DIDs under `"dids"` for DID-specific routing
- `welcome_message` overrides the `WELCOME_MESSAGE` env var per DID

---

### 4.4 `config/ab_tests.json` — A/B Testing

```json
{
  "tests": [
    {
      "test_id": "hello_greeting_style",
      "active": false,              ← Keep false during initial testing
      "traffic_split": 50,
      "control":   { "prompt_file": "prompts/hello.md" },
      "treatment": { "prompt_file": "prompts/hello_v2.md" }
    }
  ]
}
```

**What to check:** Keep all tests `"active": false` until the system is stable.

---

### 4.5 `config/settings.py` — Default Values Reference

These apply when the corresponding `.env` variable is not set.

| Setting | Default | Override via |
|---|---|---|
| AudioSocket port | `9093` | `AUDIOSOCKET_PORT` |
| HTTP port | `8080` | `PORT` or `--port` CLI arg |
| Deepgram endpointing | `500ms` | `DEEPGRAM_ENDPOINTING_MS` |
| Max concurrent calls | `500` | `MAX_CONCURRENT_CALLS` |
| Groq model (base) | `llama-3.1-8b-instant` | `GROQ_MODEL` |
| TTS provider | `elevenlabs` | `TTS_PROVIDER` |
| VAD threshold | `0.50` | `VAD_THRESHOLD` |
| Session TTL | `600s` | `REDIS_SESSION_TTL` |
| Admin key | `""` (no auth) | `ADMIN_API_KEY` |

---

## 5. Prompts — Agent Behaviour

All agent instructions live in `prompts/` as plain Markdown. Edit these to change how Priya speaks, what she asks, and how she routes.

| File | Agent | Controls |
|---|---|---|
| `prompts/hello.md` | HelloAgent | Greeting, name/mobile collection, "double" digit notation, language lock |
| `prompts/screener.md` | ScreenerAgent | Turn 0 history check, intent classification, routing rules |
| `prompts/service.md` | ServiceAgent | Complaint flow, Turn 0 skip logic, warranty table, SR number handling, escalation |
| `prompts/sales.md` | SalesAgent | Product inquiry flow, pricing guidance, dealer referral |
| `prompts/scheduler.md` | SchedulerAgent | Appointment booking, slot confirmation, reschedule handling |
| `prompts/closer.md` | CloserAgent | SR status tracking, satisfaction check, call wrap-up |

**Critical rules present in all prompts (do not remove):**
- `You are FEMALE. Always use feminine verb forms` — prevents masculine Hindi verb forms
- `NEVER say any transition phrase before handoff` — prevents "jodti hoon", "connect karti hoon"
- `Maximum 2 sentences per reply` — keeps responses phone-appropriate

**Hot-reload without restart:**
```bash
# Edit any prompt file, then:
curl -X POST http://localhost:9000/admin/reload-prompts \
     -H "X-Admin-Key: your_admin_key"
# Or wait up to 30 seconds — server polls automatically
```

---

## 6. Running the Server

```bash
source .venv/bin/activate

# Development
python main.py --port 9000

# With specific AudioSocket port
AUDIOSOCKET_PORT=9093 python main.py --port 9000

# Production with Redis
REDIS_ENABLED=true REDIS_URL=redis://localhost:6379/0 python main.py --port 9000
```

**Two servers start together:**

| Server | Default Port | Purpose |
|---|---|---|
| AudioSocket TCP | `9093` | Asterisk live call audio (PCM slin16, 8kHz, 20ms frames) |
| HTTP Admin | `9000` (via `--port`) | Health, metrics, ops dashboard, admin API |

---

## 7. Verifying the Startup Log

Reference for a healthy startup:

| Log line | Status | Action if wrong |
|---|---|---|
| `Redis disabled — using in-memory store` | OK for local dev | Set `REDIS_ENABLED=true` for production |
| `CallOutcomeStore: SQLite → ./data/calls.db` | OK | None — auto-created |
| `ChromaDB collection loaded (24 chunks)` | OK | If 0 chunks: run `python providers/rag/ingest.py` |
| `MCP server registered: dms -> https://your-dms-api.com/mcp` | WARNING | Update URL in `config/mcp_servers.json` |
| `[SileroVAD] not installed — falling back to SpectralVAD` | OK (optional) | `pip install silero-vad` for better barge-in |
| `AudioSocket TCP server listening on 0.0.0.0:9093` | OK | If missing: check port conflict |
| `FastAPI app ready (HTTP admin)` | OK | None |
| `Application startup complete` | OK | System ready |

---

## 8. Asterisk / AudioSocket Integration

Add to your Asterisk dialplan (`extensions.conf`):

```ini
[from-trunk]
exten => _X.,1,NoOp(Incoming call to Priya)
 same => n,Answer()
 same => n,Wait(1)
 same => n,AudioSocket(127.0.0.1:9093,${UNIQUEID})
 same => n,Hangup()
```

**Requirements:**
- `127.0.0.1:9093` must match `AUDIOSOCKET_PORT`
- `app_audiosocket.so` must be loaded — add to `/etc/asterisk/modules.conf`:
  ```
  load = app_audiosocket.so
  ```
- Asterisk sends audio as: PCM slin16, 8kHz, 20ms frames, mono
- The `${UNIQUEID}` becomes the call UUID tracked in logs and analytics

---

## 9. Admin & Ops Endpoints

All on the HTTP port (default `9000`).

### Public (no auth)

| Endpoint | Purpose |
|---|---|
| `GET /health` | Load balancer probe. Returns `503` during graceful shutdown drain |
| `GET /health/deep` | Deep probe: Groq + ElevenLabs + Cartesia + Redis. Returns `503` if Groq down |
| `GET /metrics` | Prometheus metrics — scrape this with your Prometheus instance |
| `GET /ops` | Ops dashboard (Bootstrap 5 + Chart.js) — call volume, agent stats, language split |
| `GET /ops/data?hours=24` | Raw ops JSON |
| `GET /ops/calls` | Recent call log |
| `GET /ops/ab-results` | A/B test bucket distribution |

### Admin (require `X-Admin-Key` header)

```bash
# Hot-reload agent prompts
curl -X POST http://localhost:9000/admin/reload-prompts \
     -H "X-Admin-Key: your_admin_key"

# Reset circuit breaker (after a provider outage)
curl -X POST http://localhost:9000/admin/circuit-breakers/reset \
     -H "X-Admin-Key: your_admin_key"

# Query audit log for a specific call
curl "http://localhost:9000/admin/audit?call_sid=abc123" \
     -H "X-Admin-Key: your_admin_key"

# Force secret refresh without restart
curl -X POST http://localhost:9000/admin/secrets/refresh \
     -H "X-Admin-Key: your_admin_key"

# View secret manager status
curl http://localhost:9000/admin/secrets/status \
     -H "X-Admin-Key: your_admin_key"

# View tenant configs
curl http://localhost:9000/admin/tenants \
     -H "X-Admin-Key: your_admin_key"
```

---

## 10. Optional Enhancements

### Neural VAD — Better Barge-In Detection

```bash
pip install silero-vad
```

After restart the log shows `Mode: neural+spectral | neural=yes`. Recommended for noisy environments or calls with heavy accents.

### HuggingFace Token — Silence Rate-Limit Warning

```env
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Redis — Required for Multi-Worker Production

```bash
# Start Redis
docker run -d -p 6379:6379 redis:7

# .env
REDIS_ENABLED=true
REDIS_URL=redis://localhost:6379/0
```

### Prometheus + Grafana Monitoring Stack

```bash
docker-compose up -d prometheus grafana
# Grafana: http://localhost:3000 (admin / admin)
# Import: monitoring/grafana_dashboard.json
# Alert rules: monitoring/alert_rules.yml
```

### Re-ingest RAG Knowledge Base

```bash
python -m providers.rag.ingest \
  --collection godrej_appliances \
  --dir knowledge_bases/

python -m providers.rag.ingest \
  --collection godrej_appliances_service \
  --dir knowledge_bases/
```

---

## 11. Architecture Quick Reference

```
Asterisk (AudioSocket TCP :9093)
         │
         ▼
StreamingPipeline ←── Deepgram Nova-3 STT (WebSocket, auto-detect language)
         │
         ▼
    Orchestrator ── session history (Redis or in-memory)
         │
         ├── HelloAgent      prompts/hello.md      (LLM only)
         ├── ScreenerAgent   prompts/screener.md   (LLM only, Turn 0 history check)
         ├── ServiceAgent    prompts/service.md    (LLM + RAG + MCP)
         ├── SalesAgent      prompts/sales.md      (LLM + RAG + MCP)
         ├── SchedulerAgent  prompts/scheduler.md  (LLM + MCP)
         └── CloserAgent     prompts/closer.md     (LLM only)
         │
         ▼
   Groq LLM ── DynamicRouter: 8b model (simple turns) / 70b model (complex turns)
         │
         ▼
ElevenLabs Flash v2.5 TTS → Cartesia Sonic-3 (auto-failover via circuit breaker)
         │
         ▼
AudioSocket PCM out → Asterisk → Caller
```

**Per-turn data flow:**
1. Asterisk streams PCM audio → Deepgram returns transcript
2. Pipeline hands transcript to active agent
3. Agent calls Groq LLM (system prompt + history + RAG context)
4. LLM response streamed sentence-by-sentence → ElevenLabs → PCM → Asterisk → Caller
5. `[HANDOFF:target]` in LLM output → orchestrator switches agent silently
6. `[END_CALL]` in LLM output → pipeline hangs up

---

## 12. Troubleshooting

### Groq 400 Bad Request

Caused by invalid message structure (empty content, consecutive same-role messages). The `_sanitize_messages()` function in `core/agent.py` auto-corrects this. If it still fires, check the error log for:

```
ERROR  Groq 400 — error_body=... messages_structure=[{role, len}...]
```

The `messages_structure` array shows each message's role and content length — look for `len=0` or consecutive same roles.

---

### Agent speaks wrong language or uses masculine Hindi verb forms

1. Check the relevant `prompts/*.md` — must contain:
   ```
   You are FEMALE. Always use feminine verb forms: "kar sakti hoon", "bata sakti hoon"
   ```
2. Check `core/streaming_pipeline.py` → `_LANG_INSTRUCTIONS["hi"]` for the LLM-level feminine enforcement.
3. Check `DEEPGRAM_LANGUAGE` — if forced to `en`, the language detector never sees Hindi.

---

### Screener re-asks question even though issue is already known

Screener's **Turn 0 History Check** (in `prompts/screener.md`) should route directly from hello history. If not working, verify the Turn 0 section appears at the top of `prompts/screener.md` before Turn 1.

---

### `screener.metadata_parse_failed` warning

Non-critical. Screener still routes but logs `confidence=low`. The brace-matching parser in `agents/screener.py → _parse_metadata()` handles normal, indented, and markdown-fenced JSON. If this appears consistently, check what the LLM is outputting for the screener turn.

---

### Appointment booking / MCP tool calls fail

1. `config/mcp_servers.json` — URL must be your actual DMS endpoint (not the placeholder)
2. `DMS_API_KEY` must be set in `.env`
3. For local dev with no DMS: set `"servers": []` in `config/mcp_servers.json`

---

### ChromaDB shows 0 chunks / RAG not returning results

```bash
python providers/rag/ingest.py
```

---

### Audio cuts in too early (agent interrupts the caller)

Increase silence tolerance in `.env`:

```env
DEEPGRAM_ENDPOINTING_MS=600    # default 500 — try 600-800 for slower speakers
VAD_THRESHOLD=0.65             # default 0.50 — higher = less sensitive barge-in
```

---

### `silero-vad not installed` at startup

Non-critical. SpectralVAD is the fallback. To fix:

```bash
pip install silero-vad
# Restart server — log will show: [CombinedVAD] Mode: neural+spectral
```

start redis : 
docker run -d --name redis -p 6379:6379 redis:7-alpine


server Pass : 5|ToH;8yJi78
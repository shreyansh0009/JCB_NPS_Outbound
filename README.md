# OSS Voice Agents

Self-hosted multi-agent voice call platform. No VAPI, no proprietary LLM services — everything runs on your own EC2 or server.

## Architecture

```
Inbound call (Twilio)
       │
       ▼
  WebSocket (FastAPI)
       │
       ▼
  AudioPipeline
   ├── STT (faster-whisper)     — speech → text
   ├── Orchestrator             — routes to the right agent
   │     ├── HelloAgent         — greet + collect name
   │     ├── ScreenerAgent      — eligibility questions
   │     ├── SchedulerAgent     — book appointment (tool-calling)
   │     └── CloserAgent        — wrap up + goodbye
   └── TTS (Piper / Kokoro)    — text → speech
```

Agents hand off to each other via `HandoffSignal`. Each agent only knows about its own job. The orchestrator routes between them based on the squad definition.

## Stack

| Component | Technology |
|-----------|-----------|
| LLM | Ollama (llama3.2, mistral, qwen2.5) |
| STT | faster-whisper (Whisper small/medium) |
| TTS | Piper TTS or Kokoro |
| Telephony | Twilio Media Streams |
| Server | FastAPI + WebSockets |
| Hosting | EC2 (any Linux instance) |

## Quick Start

### 1. Install Ollama and pull a model

```bash
curl https://ollama.ai/install.sh | sh
ollama pull llama3.2
```

### 2. Download a Piper TTS voice model

```bash
mkdir -p models
cd models
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
cd ..
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — set PIPER_MODEL_PATH, TWILIO_*, WEBHOOK_BASE_URL, WEBSOCKET_BASE_URL
```

### 4. Run

```bash
pip install -r requirements.txt
python main.py
```

### 5. Or with Docker Compose

```bash
docker-compose up
```

### 6. Configure Twilio

In your Twilio console, set your phone number's webhook:
- **Voice webhook URL**: `https://your-ec2-host.com/twilio/inbound`
- **Method**: POST

## Adding a New Agent

1. Create `agents/my_agent.py` extending `BaseAgent`
2. Add a prompt to `prompts/my_agent.md`
3. Register it in `agents/registry.py`
4. Add it to `config/squads/default_squad.json`

## Swapping Models

**LLM**: Set `OLLAMA_MODEL` to any model available in Ollama:
```
ollama pull mistral
OLLAMA_MODEL=mistral
```

Or switch to LiteLLM for access to Groq, Together AI, AWS Bedrock, etc.:
```
LLM_PROVIDER=litellm
LITELLM_MODEL=groq/llama-3.1-70b-versatile
```

**STT**: Set `WHISPER_MODEL` to `tiny`, `base`, `small`, `medium`, or `large-v3`

**TTS**: Set `TTS_PROVIDER=kokoro` and `KOKORO_MODEL_PATH` for higher quality voices

## EC2 Sizing Guide

| Use Case | Instance | Notes |
|----------|----------|-------|
| Dev/testing | t3.medium (2 vCPU, 4GB) | Whisper tiny, Ollama 3B model |
| Production (CPU) | c5.2xlarge (8 vCPU, 16GB) | Whisper small, Ollama 7B model |
| Production (GPU) | g4dn.xlarge (T4 GPU) | Whisper large, any Ollama model |

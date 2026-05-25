"""
Entry point — Montra Electric outbound NPS survey agent.

Builds LLM/STT/TTS providers, wires the streaming pipeline, and starts:
  - One AudioSocket TCP server (outbound port) — receives Asterisk audio
  - One FastAPI HTTP server — admin endpoints, dashboard, register-call-outbound

This project is outbound-only: AMI Originate → Asterisk dials customer →
AudioSocket connects on AUDIOSOCKET_OUTBOUND_PORT → pipeline runs the
Montra Electric NPS survey squad from `outbound_prompts/`.
"""
import asyncio
import contextlib
import logging
import os

import uvicorn
from dotenv import load_dotenv
# IMPORTANT: load .env BEFORE any `core.*` import. Several modules
# (streaming_pipeline, etc.) read env vars at module-import time via
# os.getenv(...) for tunable constants (e.g. BARGE_MIN_ENERGY). If
# load_dotenv runs after those imports, the constants get baked in with
# defaults and .env overrides are silently ignored.
load_dotenv(override=True)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from config.settings import Settings
from core.streaming_pipeline import StreamingPipeline
from core.latency_logger import get_latency_logger
from core.structured_logger import setup_structured_logging
from agents.registry import build_orchestrator
from api.audiosocket import start_audiosocket_server
from api.admin import router as admin_router, originate_lead
from core import outbound_call_store
from core.ami import get_ami, shutdown_ami
from core.call_queue import CallGate, DistributedCallGate
from core.outbound_campaign import CampaignManager, set_manager as set_campaign_manager
from core.response_cache import init_cache
from core import trunk_pool as _trunk_pool

setup_structured_logging(level="INFO")
logger = logging.getLogger(__name__)


def build_llm(settings: Settings):
    if settings.llm_provider == "groq":
        from providers.llm.groq_provider import GroqLLM
        return GroqLLM(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            temperature=settings.llm_temperature,
            top_p=settings.llm_top_p,
            frequency_penalty=settings.llm_frequency_penalty,
            presence_penalty=settings.llm_presence_penalty,
            timeout=settings.llm_timeout,
            max_tokens=settings.llm_max_tokens,
        )
    elif settings.llm_provider == "openai":
        from providers.llm.openai_provider import OpenAILLM
        return OpenAILLM(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            temperature=settings.llm_temperature,
            top_p=settings.llm_top_p,
            frequency_penalty=settings.llm_frequency_penalty,
            presence_penalty=settings.llm_presence_penalty,
            timeout=settings.llm_timeout,
            max_tokens=settings.llm_max_tokens,
        )
    elif settings.llm_provider == "litellm":
        from providers.llm.litellm_provider import LiteLLMProvider
        return LiteLLMProvider.from_env()
    else:
        from providers.llm.ollama import OllamaLLM
        return OllamaLLM(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            temperature=settings.llm_temperature,
            top_p=settings.llm_top_p,
            repeat_penalty=settings.llm_repeat_penalty,
            timeout=settings.llm_timeout,
            max_tokens=settings.llm_max_tokens,
        )


def build_stt(settings: Settings):
    if settings.stt_provider == "deepgram":
        from providers.stt.deepgram import DeepgramSTT
        return DeepgramSTT(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
            endpointing_ms=settings.deepgram_endpointing_ms,
        )
    else:
        from providers.stt.whisper import WhisperSTT
        return WhisperSTT(
            model_size=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute,
        )


def build_tts(settings: Settings):
    if settings.tts_provider == "elevenlabs":
        from providers.tts.elevenlabs import ElevenLabsTTS
        voice_language_map = {
            lang: voice_id
            for lang, voice_id in {
                "hi": settings.elevenlabs_voice_hi,
                "bn": settings.elevenlabs_voice_bn,
                "te": settings.elevenlabs_voice_te,
                "mr": settings.elevenlabs_voice_mr,
                "ta": settings.elevenlabs_voice_ta,
                "gu": settings.elevenlabs_voice_gu,
                "kn": settings.elevenlabs_voice_kn,
                "pa": settings.elevenlabs_voice_pa,
                "ml": settings.elevenlabs_voice_ml,
                "or": settings.elevenlabs_voice_or,
            }.items()
            if voice_id
        }
        return ElevenLabsTTS(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_voice_id,
            model=settings.elevenlabs_model,
            stability=settings.elevenlabs_stability,
            similarity_boost=settings.elevenlabs_similarity_boost,
            style=settings.elevenlabs_style,
            voice_language_map=voice_language_map,
        )
    elif settings.tts_provider == "cartesia":
        from providers.tts.cartesia import CartesiaTTS
        return CartesiaTTS(
            api_key=settings.cartesia_api_key,
            voice_id=settings.cartesia_voice_id,
            model=settings.cartesia_model,
            number_speed=settings.cartesia_number_speed,
        )
    elif settings.tts_provider == "kokoro":
        from providers.tts.kokoro import KokoroTTS
        return KokoroTTS(model_path=settings.kokoro_model_path, voice=settings.kokoro_voice)
    else:
        from providers.tts.piper import PiperTTS
        return PiperTTS(model_path=settings.piper_model_path)


def create_app(settings: Settings, call_gate: "CallGate | None" = None) -> FastAPI:
    app = FastAPI(title="Voice Agent — Outbound (Montra Electric)", version="3.0.0")

    app.include_router(admin_router)  # /admin/health, /api/outbound/*, /admin/outbound

    @app.get("/health")
    def health():
        body = {
            "status": "ok",
            "llm": settings.llm_provider,
            "stt": settings.stt_provider,
            "tts": settings.tts_provider,
            "audiosocket_port": settings.audiosocket_outbound_port,
        }
        if call_gate is not None:
            body["capacity"] = call_gate.get_status()
        try:
            pool = _trunk_pool.get_pool()
            if pool is not None:
                body["trunks"] = pool.status()
        except Exception:
            pass
        try:
            from core.rate_limiter import TenantRateLimiter
            body["rate_limits"] = TenantRateLimiter.status()
        except Exception:
            pass
        return body

    def _first_non_empty(params: dict[str, str], keys: tuple[str, ...]) -> str:
        for k in keys:
            v = (params.get(k) or "").strip()
            if v:
                return v
        return ""

    @app.api_route("/api/asterisk/register-call-outbound", methods=["GET", "POST"])
    async def register_call_outbound(
        request: Request,
        uuid: str = "",
        did: str = "",
        callerid: str = "",
    ):
        """
        Called by Asterisk [outbound-ai] dialplan just before AudioSocket.
        Lead context was already written at /api/outbound/originate time, so
        this is mostly a logging confirmation — but we also refresh the _ts
        of the stashed entry so the pipeline's recency fallback favors it.
        """
        q = {k.lower(): v for k, v in request.query_params.items()}
        resolved_uuid = (
            uuid.strip()
            or _first_non_empty(q, ("uuid", "uniqueid", "channelid", "call_uuid", "id"))
        )
        resolved_did = (
            did.strip()
            or _first_non_empty(q, ("did", "to", "dnis", "called", "destination"))
        )

        logger.info(
            f"Asterisk register-call-outbound: uuid={resolved_uuid} did={resolved_did} callerid={callerid}"
        )
        entry = await outbound_call_store.peek(resolved_uuid) if resolved_uuid else None
        if entry is not None:
            # Re-stamp by re-putting (works for both in-memory and Redis backend)
            if resolved_did and not entry.get("did"):
                entry["did"] = resolved_did
            await outbound_call_store.put(resolved_uuid, entry)
        else:
            # Asterisk sent its own UNIQUEID instead of AUDIOSOCKET_UUID — refresh
            # the most recent store entry so the pipeline's recency fallback
            # still matches when AudioSocket connects with the correct UUID.
            newest = await outbound_call_store.peek_newest()
            if newest is not None:
                # Find its uuid via snapshot, re-put to refresh _ts
                snap = await outbound_call_store.snapshot()
                # snapshot returns a dict {uuid: entry}; pick the entry that matches `newest`
                for k, v in snap.items():
                    if v is newest or (isinstance(v, dict) and v.get("_ts") == newest.get("_ts")):
                        await outbound_call_store.put(k, newest)
                        logger.info(
                            f"register-call-outbound: UUID {resolved_uuid!r} not in store; "
                            f"refreshed _ts on newest entry ({k}) for recency fallback"
                        )
                        break
        return {
            "status": "ok",
            "uuid": resolved_uuid,
            "did": resolved_did,
            "known": entry is not None,
        }

    @app.get("/outbound", response_class=HTMLResponse)
    @app.get("/outbound_dashboard.html", response_class=HTMLResponse)
    def serve_outbound_dashboard():
        import os
        html_path = os.path.join(os.path.dirname(__file__), "api", "outbound_dashboard.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                return f.read()
        return "Dashboard file not found."

    logger.info("FastAPI app ready (HTTP admin)")
    return app


async def main():
    settings = Settings.from_env()

    if not settings.outbound_enabled:
        raise RuntimeError(
            "OUTBOUND_ENABLED must be true. This project is outbound-only "
            "(Montra Electric NPS survey)."
        )
    if not settings.sip_trunk:
        raise RuntimeError("SIP_TRUNK must be configured in .env")

    llm = build_llm(settings)
    stt = build_stt(settings)
    tts = build_tts(settings)

    logger.info(f"LLM={settings.llm_provider} STT={settings.stt_provider} TTS={settings.tts_provider}")

    # Single orchestrator — Montra Electric NPS survey squad from outbound_prompts/
    orchestrator = build_orchestrator(
        settings.outbound_squad_path, llm, prompts_dir="outbound_prompts"
    )
    logger.info(
        f"Orchestrator built from 'outbound_prompts/' "
        f"squad='{settings.outbound_squad_path}'"
    )

    # ── Prompt fingerprints (so logs unambiguously show which revision is live)
    # ── + background polling so disk edits to outbound_prompts/*.md take effect
    # ── within ~30s without a PM2 restart.
    prompt_loader = getattr(orchestrator, "prompt_loader", None)
    if prompt_loader is not None:
        fps = prompt_loader.fingerprints()
        for name, fp in fps.items():
            logger.info(f"[PromptLoader] {name}.md fingerprint={fp}")
        asyncio.create_task(prompt_loader.start_polling())

    # Single pipeline. Per-call welcome / lead context comes from
    # outbound_call_store, populated by /api/outbound/originate.
    # Configure outbound call-context store (Redis-backed if enabled, else in-memory).
    # This must run before any /api/outbound/originate call.
    await outbound_call_store.init_from_settings(
        redis_enabled=settings.redis_enabled,
        redis_url=settings.redis_url,
        ttl_seconds=settings.outbound_store_ttl_s,
    )

    # Configure SIP trunk pool (failover + caller-ID rotation). When TRUNKS env
    # is set, multi-trunk + multi-caller-id is active. Otherwise falls back to
    # single SIP_TRUNK / OUTBOUND_CALLER_ID (legacy Phase 1/2 behaviour).
    try:
        from core.trunk_pool import TrunkPool as _TrunkPool
        _trunk_pool.configure(_TrunkPool.from_settings(settings))
        logger.info(f"TrunkPool configured: {_trunk_pool.get_pool().status()}")
    except Exception as e:
        logger.warning(f"TrunkPool init skipped ({e}); originate_lead will lazy-bootstrap")

    # OpenTelemetry tracing (opt-in). When OTEL_ENABLED=false the shim is a no-op.
    try:
        from core.tracing import init_tracing
        init_tracing(
            enabled=settings.otel_enabled,
            service_name=settings.otel_service_name,
            endpoint=settings.otel_exporter_otlp_endpoint,
        )
    except Exception as e:
        logger.warning(f"OTel init skipped ({e})")

    # Per-tenant rate limiting (token bucket). Single-tenant; uses global defaults.
    try:
        from core.rate_limiter import TenantRateLimiter
        TenantRateLimiter.configure(
            defaults=(settings.rate_limit_capacity, settings.rate_limit_rate_per_sec),
            per_tenant={},
        )
    except Exception as e:
        logger.warning(f"TenantRateLimiter init skipped ({e})")

    # Initialize LLM/TTS/RAG response cache. Cache stays disabled at the agent
    # call sites until LLM_CACHE_ENABLED=true is set — this just provisions the
    # Redis-backed singleton so opt-in flips immediately when the env is set.
    redis_client_for_cache = None
    if settings.redis_enabled:
        try:
            import redis.asyncio as aioredis
            redis_client_for_cache = await aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=False,  # ResponseCache base64-encodes audio
                socket_connect_timeout=3,
                socket_timeout=3,
                retry_on_timeout=True,
                health_check_interval=30,
            )
            await redis_client_for_cache.ping()
            logger.info(f"ResponseCache: Redis backend connected ({settings.redis_url})")
        except Exception as e:
            logger.warning(f"ResponseCache: Redis unavailable ({e}); using local LRU only")
            redis_client_for_cache = None
    init_cache(redis_client=redis_client_for_cache)

    pipeline = StreamingPipeline(
        stt=stt,
        tts=tts,
        llm=llm,
        orchestrator=orchestrator,
        welcome_message="",  # always overridden per-call by lead context
        audio_out_max_frames=settings.audio_out_max_frames,
        idle_timeout_s=settings.call_idle_timeout_s,
    )

    # Start latency logger (background writer)
    lat = get_latency_logger()
    await lat.start()
    logger.info("LatencyLogger appending to latency.log")

    # Per-process concurrency gate. Sized for ~30-50 MB/call on this worker;
    # adjust MAX_CONCURRENT_CALLS in env when scaling vertically. Calls beyond
    # the cap are queued briefly then HANGUP'd so Asterisk can retry.
    local_gate = CallGate(
        max_concurrent=settings.max_concurrent_calls,
        queue_timeout_s=settings.call_queue_timeout_s,
        max_queue_size=settings.call_queue_size,
    )

    # Optional cross-pod ceiling. When GLOBAL_MAX_CONCURRENT_CALLS > 0 and
    # Redis is reachable, all pods share a single global counter so deploying
    # 4 replicas with local_max=50 doesn't allow 200 concurrent — the global
    # cap wins. Falls back transparently to local-only if Redis is down.
    call_gate = local_gate
    if settings.global_max_concurrent_calls > 0 and settings.redis_enabled:
        try:
            import redis.asyncio as aioredis
            _gate_redis = await aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
                retry_on_timeout=True,
                health_check_interval=30,
            )
            await _gate_redis.ping()
            call_gate = DistributedCallGate(
                local=local_gate,
                global_max=settings.global_max_concurrent_calls,
                redis_client=_gate_redis,
                slot_ttl_s=int(settings.call_max_duration_s) + 60,
            )
        except Exception as e:
            logger.warning(
                f"DistributedCallGate disabled ({e}); using local-only CallGate"
            )

    # Start AudioSocket TCP server on the outbound port
    tcp_server = await start_audiosocket_server(
        pipeline,
        host=settings.audiosocket_host,
        port=settings.audiosocket_outbound_port,
        call_gate=call_gate,
        max_call_duration_s=settings.call_max_duration_s,
    )

    # Lazy-connect AMI (warms singleton so first Originate is fast)
    try:
        await get_ami(settings)
    except Exception as e:
        logger.warning(f"AMI connect failed — outbound originate will retry on demand: {e}")

    # Campaign manager dials via the same path as /api/outbound/originate
    async def _dial(lead):
        return await originate_lead(lead)

    set_campaign_manager(CampaignManager(dial_fn=_dial))

    logger.info(
        f"Outbound enabled: AudioSocket :{settings.audiosocket_outbound_port}, "
        f"AMI {settings.ami_host}:{settings.ami_port}, trunk={settings.sip_trunk}"
    )

    # Start FastAPI HTTP server
    app = create_app(settings, call_gate=call_gate)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_config=None)
    http_server = uvicorn.Server(config)

    logger.info(
        f"Servers running:\n"
        f"  AudioSocket TCP → {settings.audiosocket_host}:{settings.audiosocket_outbound_port}\n"
        f"  HTTP Admin      → {settings.host}:{settings.port}"
    )

    try:
        await http_server.serve()
    finally:
        tcp_server.close()
        with contextlib.suppress(asyncio.CancelledError):
            await tcp_server.wait_closed()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_ami()
        with contextlib.suppress(asyncio.CancelledError):
            await lat.stop()
        logger.info("LatencyLogger stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user (Ctrl+C)")

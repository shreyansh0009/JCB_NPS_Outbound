"""
Application settings — loaded from environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # LLM — default: Groq (fastest)
    llm_provider: str = "groq"            # "groq" | "openai" | "ollama" | "litellm"
    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"  # 750 tok/s vs 280 tok/s on 70b
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    llm_temperature: float = 0.25
    llm_top_p: float = 0.85
    llm_frequency_penalty: float = 0.35
    llm_presence_penalty: float = 0.05
    llm_repeat_penalty: float = 1.08
    llm_max_tokens: int = 90
    llm_timeout: int = 12

    # STT — default: Deepgram (streaming, ~200ms latency)
    stt_provider: str = "deepgram"        # "deepgram" | "whisper"
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    deepgram_language: str = ""          # empty = auto-detect multilingual (detect_language=true)
    deepgram_endpointing_ms: int = 150    # lowered from 300ms — faster end-of-speech detection
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute: str = "int8"

    # TTS — default: Cartesia Sonic (streaming, ~80ms first chunk)
    tts_provider: str = "elevenlabs"        # "elevenlabs" | "piper" | "kokoro"

    # TTS — ElevenLabs Flash v2.5
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # Rachel (English default)
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_stability: float = 0.40
    elevenlabs_similarity_boost: float = 0.85
    elevenlabs_style: float = 0.15
    elevenlabs_speed: float = 0.95
    # Per-language voice IDs (optional — Flash v2.5 works multilingually on any voice)
    elevenlabs_voice_hi: str = ""   # Hindi
    elevenlabs_voice_bn: str = ""   # Bengali
    elevenlabs_voice_te: str = ""   # Telugu
    elevenlabs_voice_mr: str = ""   # Marathi
    elevenlabs_voice_ta: str = ""   # Tamil
    elevenlabs_voice_gu: str = ""   # Gujarati
    elevenlabs_voice_kn: str = ""   # Kannada
    elevenlabs_voice_pa: str = ""   # Punjabi
    elevenlabs_voice_ml: str = ""   # Malayalam
    elevenlabs_voice_or: str = ""   # Odia

    # Welcome message (spoken by agent when call connects)
    welcome_message: str = "Hello! Thank you for calling. How can I assist you today?"

    cartesia_api_key: str = ""
    cartesia_voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"
    cartesia_model: str = "sonic-2"
    cartesia_number_speed: float = 1.5

    piper_model_path: str = ""
    kokoro_model_path: str = ""
    kokoro_voice: str = "af_heart"

    # Telephony — Asterisk AudioSocket
    audiosocket_host: str = "0.0.0.0"
    audiosocket_port: int = 9093

    # Concurrency control (per-process)
    max_concurrent_calls: int = 50      # CallGate semaphore size
    call_queue_size: int = 100          # CallGate wait-queue size
    call_queue_timeout_s: float = 15.0  # Wait at-capacity before reject
    call_idle_timeout_s: float = 60.0   # No-audio timeout before hangup
    call_max_duration_s: float = 600.0  # Hard cap per call (10 min)
    audio_out_max_frames: int = 200     # Bounded TTS→Asterisk queue (4s @ 20ms/frame)
    # Cross-pod global ceiling. 0 disables the distributed gate.
    global_max_concurrent_calls: int = 0

    # HTTP client pool sizing (Groq, Cartesia)
    httpx_max_connections: int = 600
    httpx_max_keepalive: int = 200
    httpx_keepalive_expiry: int = 30

    # Redis (shared state for horizontal scaling — outbound_call_store, sessions)
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    outbound_store_ttl_s: int = 120

    # AMI connection pool (multiple parallel reader loops for originate burst)
    ami_pool_size: int = 4

    # LLM response cache (stateless turns only; opt-in)
    llm_cache_enabled: bool = False

    # Per-tenant rate limiting (token bucket). Defaults: 10 burst, 2/sec steady (~120/min).
    rate_limit_capacity: float = 10.0
    rate_limit_rate_per_sec: float = 2.0

    # OpenTelemetry (opt-in)
    otel_enabled: bool = False
    otel_service_name: str = "montra-outbound"
    otel_exporter_otlp_endpoint: str = ""

    # Outbound calling (Asterisk AMI + 2nd AudioSocket server)
    outbound_enabled: bool = False
    audiosocket_outbound_port: int = 9092
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_user: str = ""
    ami_secret: str = ""
    sip_trunk: str = ""
    sip_prefix: str = ""
    outbound_caller_id: str = ""
    outbound_squad_path: str = str(Path(__file__).parent / "squads" / "appliances_outbound_squad.json")
    outbound_welcome_template: str = (
        "Hello {name}, this is Priya from Godrej Appliances regarding your {product}. "
        "Do you have a minute to talk?"
    )

    # Legacy Twilio (unused with AudioSocket)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    webhook_base_url: str = ""
    websocket_base_url: str = ""

    # Squad
    squad_path: str = str(Path(__file__).parent / "squads" / "appliances_squad.json")
    inbound_prompts_dir: str = "automotivepropmts"

    # Salesforce (password grant flow)
    sf_instance_url: str = ""       # e.g. "https://orgfarm-xxx.develop.my.salesforce.com"
    sf_client_id: str = ""
    sf_client_secret: str = ""
    sf_username: str = ""
    sf_password: str = ""           # password + security token concatenated
    sf_access_token: str = ""       # optional pre-set token (fallback if no username)

    # Cloudinary (call recording upload)
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""

    # Email (Resend)
    resend_api_key: str = ""
    resend_from_email: str = "noreply@crmlanding.co.in"

    # WhatsApp Business API
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""       # Facebook phone number ID
    whatsapp_template_name: str = "godrej_service_demo"

    # Exotel (Indian telephony)
    exotel_api_key: str = ""
    exotel_api_token: str = ""
    exotel_account_sid: str = ""
    exotel_subdomain: str = "@api.in.exotel.com"
    exotel_app_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8080")),

            llm_provider=os.getenv("LLM_PROVIDER", "groq"),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2"),
            llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.25")),
            llm_top_p=float(os.getenv("LLM_TOP_P", "0.85")),
            llm_frequency_penalty=float(os.getenv("LLM_FREQUENCY_PENALTY", "0.35")),
            llm_presence_penalty=float(os.getenv("LLM_PRESENCE_PENALTY", "0.05")),
            llm_repeat_penalty=float(os.getenv("LLM_REPEAT_PENALTY", "1.08")),
            llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "90")),
            llm_timeout=int(os.getenv("LLM_TIMEOUT", "12")),

            stt_provider=os.getenv("STT_PROVIDER", "deepgram"),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-2"),
            deepgram_language=os.getenv("DEEPGRAM_LANGUAGE", "").strip(),  # empty = auto-detect
            deepgram_endpointing_ms=int(os.getenv("DEEPGRAM_ENDPOINTING_MS", "300")),
            whisper_model=os.getenv("WHISPER_MODEL", "small"),
            whisper_device=os.getenv("WHISPER_DEVICE", "cpu"),
            whisper_compute=os.getenv("WHISPER_COMPUTE", "int8"),

            tts_provider=os.getenv("TTS_PROVIDER", "elevenlabs"),

            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
            elevenlabs_model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            elevenlabs_stability=float(os.getenv("ELEVENLABS_STABILITY", "0.5")),
            elevenlabs_similarity_boost=float(os.getenv("ELEVENLABS_SIMILARITY_BOOST", "0.75")),
            elevenlabs_style=float(os.getenv("ELEVENLABS_STYLE", "0.0")),
            elevenlabs_speed=float(os.getenv("ELEVENLABS_SPEED", "1.0")),
            elevenlabs_voice_hi=os.getenv("ELEVENLABS_VOICE_HI", ""),
            elevenlabs_voice_bn=os.getenv("ELEVENLABS_VOICE_BN", ""),
            elevenlabs_voice_te=os.getenv("ELEVENLABS_VOICE_TE", ""),
            elevenlabs_voice_mr=os.getenv("ELEVENLABS_VOICE_MR", ""),
            elevenlabs_voice_ta=os.getenv("ELEVENLABS_VOICE_TA", ""),
            elevenlabs_voice_gu=os.getenv("ELEVENLABS_VOICE_GU", ""),
            elevenlabs_voice_kn=os.getenv("ELEVENLABS_VOICE_KN", ""),
            elevenlabs_voice_pa=os.getenv("ELEVENLABS_VOICE_PA", ""),
            elevenlabs_voice_ml=os.getenv("ELEVENLABS_VOICE_ML", ""),
            elevenlabs_voice_or=os.getenv("ELEVENLABS_VOICE_OR", ""),

            welcome_message=os.getenv(
                "WELCOME_MESSAGE",
                "Hello! Thank you for calling. How can I assist you today?"
            ),

            cartesia_api_key=os.getenv("CARTESIA_API_KEY", ""),
            cartesia_voice_id=os.getenv("CARTESIA_VOICE_ID", "a0e99841-438c-4a64-b679-ae501e7d6091"),
            cartesia_model=os.getenv("CARTESIA_MODEL", "sonic-2"),
            cartesia_number_speed=float(os.getenv("CARTESIA_NUMBER_SPEED", "1.5")),

            piper_model_path=os.getenv("PIPER_MODEL_PATH", ""),
            kokoro_model_path=os.getenv("KOKORO_MODEL_PATH", ""),
            kokoro_voice=os.getenv("KOKORO_VOICE", "af_heart"),

            audiosocket_host=os.getenv("AUDIOSOCKET_HOST", "0.0.0.0"),
            audiosocket_port=int(os.getenv("AUDIOSOCKET_PORT", "9093")),

            max_concurrent_calls=int(os.getenv("MAX_CONCURRENT_CALLS", "50")),
            call_queue_size=int(os.getenv("CALL_QUEUE_SIZE", "100")),
            call_queue_timeout_s=float(os.getenv("CALL_QUEUE_TIMEOUT_S", "15.0")),
            call_idle_timeout_s=float(os.getenv("CALL_IDLE_TIMEOUT_S", "60.0")),
            call_max_duration_s=float(os.getenv("CALL_MAX_DURATION_S", "600.0")),
            audio_out_max_frames=int(os.getenv("AUDIO_OUT_MAX_FRAMES", "200")),
            global_max_concurrent_calls=int(os.getenv("GLOBAL_MAX_CONCURRENT_CALLS", "0")),
            httpx_max_connections=int(os.getenv("HTTPX_MAX_CONNECTIONS", "600")),
            httpx_max_keepalive=int(os.getenv("HTTPX_MAX_KEEPALIVE", "200")),
            httpx_keepalive_expiry=int(os.getenv("HTTPX_KEEPALIVE_EXPIRY", "30")),
            redis_enabled=os.getenv("REDIS_ENABLED", "false").lower() in ("1", "true", "yes"),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            outbound_store_ttl_s=int(os.getenv("OUTBOUND_STORE_TTL_S", "120")),
            ami_pool_size=int(os.getenv("AMI_POOL_SIZE", "4")),
            llm_cache_enabled=os.getenv("LLM_CACHE_ENABLED", "false").lower() in ("1", "true", "yes"),
            rate_limit_capacity=float(os.getenv("RATE_LIMIT_CAPACITY", "10")),
            rate_limit_rate_per_sec=float(os.getenv("RATE_LIMIT_RATE_PER_SEC", "2")),
            otel_enabled=os.getenv("OTEL_ENABLED", "false").lower() in ("1", "true", "yes"),
            otel_service_name=os.getenv("OTEL_SERVICE_NAME", "montra-outbound"),
            otel_exporter_otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),

            outbound_enabled=os.getenv("OUTBOUND_ENABLED", "false").lower() in ("1", "true", "yes"),
            audiosocket_outbound_port=int(os.getenv("AUDIOSOCKET_OUTBOUND_PORT", "9092")),
            ami_host=os.getenv("AMI_HOST", "127.0.0.1"),
            ami_port=int(os.getenv("AMI_PORT", "5038")),
            ami_user=os.getenv("AMI_USER", ""),
            ami_secret=os.getenv("AMI_SECRET", ""),
            sip_trunk=os.getenv("SIP_TRUNK", ""),
            sip_prefix=os.getenv("SIP_PREFIX", ""),
            outbound_caller_id=os.getenv("OUTBOUND_CALLER_ID", ""),
            outbound_squad_path=os.getenv(
                "OUTBOUND_SQUAD_PATH",
                str(Path(__file__).parent / "squads" / "appliances_outbound_squad.json"),
            ),
            outbound_welcome_template=os.getenv(
                "OUTBOUND_WELCOME_TEMPLATE",
                "Hello {name}, this is Priya from Godrej Appliances regarding your {product}. "
                "Do you have a minute to talk?",
            ),

            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            webhook_base_url=os.getenv("WEBHOOK_BASE_URL", ""),
            websocket_base_url=os.getenv("WEBSOCKET_BASE_URL", ""),

            squad_path=os.getenv(
                "SQUAD_PATH",
                str(Path(__file__).parent / "squads" / "appliances_squad.json"),
            ),
            inbound_prompts_dir=os.getenv("INBOUND_PROMPTS_DIR", "automotivepropmts"),
            sf_instance_url=os.getenv("SF_INSTANCE_URL", ""),
            sf_client_id=os.getenv("SF_CLIENT_ID", ""),
            sf_client_secret=os.getenv("SF_CLIENT_SECRET", ""),
            sf_username=os.getenv("SF_USERNAME", ""),
            sf_password=os.getenv("SF_PASSWORD", ""),
            sf_access_token=os.getenv("SF_ACCESS_TOKEN", ""),

            cloudinary_cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
            cloudinary_api_key=os.getenv("CLOUDINARY_API_KEY", ""),
            cloudinary_api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),

            resend_api_key=os.getenv("RESEND_API_KEY", ""),
            resend_from_email=os.getenv("RESEND_FROM_EMAIL", "noreply@crmlanding.co.in"),

            whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
            whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
            whatsapp_template_name=os.getenv("WHATSAPP_TEMPLATE_NAME", "godrej_service_demo"),

            exotel_api_key=os.getenv("EXOTEL_API_KEY", ""),
            exotel_api_token=os.getenv("EXOTEL_API_TOKEN", ""),
            exotel_account_sid=os.getenv("EXOTEL_ACCOUNT_SID", ""),
            exotel_subdomain=os.getenv("EXOTEL_SUBDOMAIN", "@api.in.exotel.com"),
            exotel_app_id=os.getenv("EXOTEL_APP_ID", ""),
        )

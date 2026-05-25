"""
Sprint 8: Secret rotation without restart.

Secrets are loaded from environment variables initially, then refreshed
periodically from the configured backend (env | ssm | vault).

When a secret value changes, registered callbacks fire so providers can
rebuild their HTTP clients with the new key.

SIGHUP triggers an immediate refresh (production rotation workflow:
  1. Update secret in SSM/Vault
  2. kill -HUP <pid>   ← instant pick-up, no restart
  3. Old in-flight calls complete with old key (httpx connection pool)
  4. New calls use new key

Usage:
    secrets = SecretManager()
    await secrets.start()
    key = secrets.get("GROQ_API_KEY")
    secrets.on_change("GROQ_API_KEY", rebuild_groq_client)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_MANAGED_KEYS = [
    "GROQ_API_KEY", "ELEVENLABS_API_KEY", "CARTESIA_API_KEY",
    "DEEPGRAM_API_KEY", "ADMIN_API_KEY", "REDIS_URL",
    "CRM_API_KEY", "AUDIT_DB_PATH",
]
_REFRESH_INTERVAL_S = int(os.environ.get("SECRET_REFRESH_INTERVAL_S", "300"))


class SecretManager:
    """Thread-safe secret manager with rotation callbacks."""

    _instance: "SecretManager | None" = None

    def __init__(self):
        self._secrets: dict[str, str] = {}
        self._callbacks: dict[str, list[Callable[[str, str], Awaitable[None]]]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._backend = os.environ.get("SECRET_BACKEND", "env")
        self._refresh_count = 0

    @classmethod
    def get(cls) -> "SecretManager | None":
        return cls._instance

    @classmethod
    def set_instance(cls, inst: "SecretManager") -> None:
        cls._instance = inst

    async def start(self) -> None:
        await self._load()
        self._task = asyncio.create_task(self._refresh_loop(), name="secret-refresh")
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.create_task(self._load()))
            logger.info("[SecretManager] SIGHUP handler registered")
        except (OSError, NotImplementedError):
            pass  # Windows
        logger.info(f"[SecretManager] Started backend={self._backend} refresh={_REFRESH_INTERVAL_S}s")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def get(self, key: str, default: str = "") -> str:
        return self._secrets.get(key) or os.environ.get(key, default)

    def on_change(self, key: str, cb: Callable[[str, str], Awaitable[None]]) -> None:
        self._callbacks.setdefault(key, []).append(cb)

    async def _load(self) -> None:
        async with self._lock:
            if self._backend == "ssm":
                await self._load_ssm()
            elif self._backend == "vault":
                await self._load_vault()
            else:
                await self._load_env()
            self._refresh_count += 1

    async def _load_env(self) -> None:
        for key in _MANAGED_KEYS:
            val = os.environ.get(key, "")
            old = self._secrets.get(key, "")
            if val and val != old:
                self._secrets[key] = val
                if old:  # skip callbacks on first load
                    await self._fire(key, val)

    async def _load_ssm(self) -> None:
        try:
            import boto3
            prefix = os.environ.get("SSM_PREFIX", "/aivoice/")
            client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
            paginator = client.get_paginator("get_parameters_by_path")
            for page in paginator.paginate(Path=prefix, WithDecryption=True):
                for param in page["Parameters"]:
                    key = param["Name"].replace(prefix, "").upper().replace("-", "_")
                    val = param["Value"]
                    old = self._secrets.get(key, "")
                    if val != old:
                        self._secrets[key] = val
                        if old:
                            await self._fire(key, val)
        except Exception as e:
            logger.error(f"[SecretManager] SSM error: {e}")

    async def _load_vault(self) -> None:
        try:
            import httpx
            addr = os.environ.get("VAULT_ADDR", "http://localhost:8200")
            token = os.environ.get("VAULT_TOKEN", "")
            path = os.environ.get("VAULT_SECRET_PATH", "secret/data/aivoice")
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{addr}/v1/{path}",
                                headers={"X-Vault-Token": token}, timeout=5.0)
                r.raise_for_status()
                for key, val in r.json().get("data", {}).get("data", {}).items():
                    key_u = key.upper()
                    old = self._secrets.get(key_u, "")
                    if val != old:
                        self._secrets[key_u] = val
                        if old:
                            await self._fire(key_u, val)
        except Exception as e:
            logger.error(f"[SecretManager] Vault error: {e}")

    async def _fire(self, key: str, val: str) -> None:
        logger.info(f"[SecretManager] '{key}' rotated — firing {len(self._callbacks.get(key,[]))} callbacks")
        for cb in self._callbacks.get(key, []):
            try:
                await cb(key, val)
            except Exception as e:
                logger.error(f"[SecretManager] Callback error: {e}")

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL_S)
            try:
                await self._load()
                logger.debug(f"[SecretManager] Refreshed (count={self._refresh_count})")
            except Exception as e:
                logger.error(f"[SecretManager] Refresh error: {e}")

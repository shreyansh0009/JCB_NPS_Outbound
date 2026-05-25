"""
Sprint 7: Multi-tenant / multi-DID routing.

Each DID (dialed number or account identifier) maps to a tenant config.
Tenants can override: squad file, welcome message, agent branding.

Config: config/tenants.json
Default tenant used when DID is unknown.

tenants.json example:
{
  "default": {
    "name": "Godrej Appliances",
    "squad_path": "config/squads/godrej.json",
    "welcome_message": "",
    "language_default": "hi"
  },
  "dids": {
    "+911800123456": {
      "tenant_id": "godrej_premium",
      "name": "Godrej Premium Support",
      "squad_path": "config/squads/godrej.json",
      "welcome_message": "नमस्ते! Godrej Premium Support में आपका स्वागत है।",
      "language_default": "hi"
    }
  }
}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TenantConfig:
    tenant_id: str = "default"
    name: str = "Default Tenant"
    squad_path: str = "config/squads/godrej.json"
    welcome_message: str = ""
    language_default: str = "hi"
    extra: dict = field(default_factory=dict)


class TenantManager:
    """Maps incoming DIDs to tenant configurations."""

    _instance: "TenantManager | None" = None

    def __init__(self, config_path: str = "config/tenants.json"):
        self._path = Path(config_path)
        self._default = TenantConfig()
        self._did_map: dict[str, TenantConfig] = {}
        self._load()

    @classmethod
    def get(cls) -> "TenantManager | None":
        return cls._instance

    @classmethod
    def set_instance(cls, instance: "TenantManager") -> None:
        cls._instance = instance

    def _load(self) -> None:
        if not self._path.exists():
            logger.info("[TenantManager] No tenants.json — using default tenant")
            return
        with open(self._path) as f:
            data = json.load(f)

        # Default tenant
        d = data.get("default", {})
        self._default = TenantConfig(
            tenant_id="default",
            name=d.get("name", "Default"),
            squad_path=d.get("squad_path", "config/squads/godrej.json"),
            welcome_message=d.get("welcome_message", ""),
            language_default=d.get("language_default", "hi"),
            extra=d.get("extra", {}),
        )

        # DID → tenant mapping
        self._did_map.clear()
        for did, cfg in data.get("dids", {}).items():
            self._did_map[did] = TenantConfig(
                tenant_id=cfg.get("tenant_id", did),
                name=cfg.get("name", did),
                squad_path=cfg.get("squad_path", self._default.squad_path),
                welcome_message=cfg.get("welcome_message", self._default.welcome_message),
                language_default=cfg.get("language_default", self._default.language_default),
                extra=cfg.get("extra", {}),
            )

        logger.info(
            f"[TenantManager] Loaded {len(self._did_map)} DIDs, "
            f"default='{self._default.name}'"
        )

    def lookup(self, did: str) -> TenantConfig:
        """Return tenant config for a DID. Falls back to default."""
        if did and did in self._did_map:
            tenant = self._did_map[did]
            logger.debug(f"[TenantManager] DID {did} → tenant '{tenant.tenant_id}'")
            return tenant
        return self._default

    def reload(self) -> None:
        self._load()

    @property
    def tenant_count(self) -> int:
        return len(self._did_map)

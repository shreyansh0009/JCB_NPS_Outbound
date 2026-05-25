"""
Sprint 7: CRM base interface — customer history prefetch.

Implementations: MockCRMProvider (default), real CRM via HTTP adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CustomerRecord:
    """Pre-populated customer data from CRM."""
    found: bool = False
    name: str = ""
    mobile: str = ""
    email: str = ""
    account_tier: str = "standard"          # standard | premium | vip
    products: list[str] = field(default_factory=list)   # ["AC-GR3000", "Fridge-GEO24"]
    open_complaints: int = 0
    last_complaint_desc: str = ""
    preferred_language: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class BaseCRMProvider(ABC):
    @abstractmethod
    async def lookup(self, mobile: str) -> CustomerRecord:
        """Look up a customer by mobile number. Returns CustomerRecord(found=False) if not found."""
        ...

    async def close(self) -> None:
        pass

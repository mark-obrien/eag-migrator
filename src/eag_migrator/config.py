"""Runtime configuration, read from the environment (.env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Project root is three levels up from this file: src/eag_migrator/config.py
ROOT = Path(__file__).resolve().parents[2]

PROFILES_DIR = ROOT / "profiles"
REPORTS_DIR = ROOT / "reports"
STATE_DIR = ROOT / "state"
CONFIG_DIR = ROOT / "config"

DEFAULT_REDACT = "password,passwd,pwd,hash,salt,token,secret,ssn,social,card,cvv,api_key,auth"


@dataclass
class Settings:
    v2_url: str = ""
    v2_schema: str | None = None
    v3_url: str = ""
    v3_schema: str | None = None

    v3_api_base_url: str | None = None
    v3_api_token: str | None = None
    v3_api_timeout: int = 30

    batch_size: int = 500
    on_error: str = "record"
    redact_patterns: list[str] = field(default_factory=lambda: DEFAULT_REDACT.split(","))

    def redactor(self) -> re.Pattern[str] | None:
        """Compile the redaction patterns into one case-insensitive regex."""
        pats = [p.strip() for p in self.redact_patterns if p.strip()]
        if not pats:
            return None
        return re.compile("|".join(re.escape(p) for p in pats), re.IGNORECASE)

    def url_for(self, side: str) -> str:
        url = self.v2_url if side == "v2" else self.v3_url
        if not url:
            raise ValueError(
                f"No database URL configured for {side}. "
                f"Set {side.upper()}_DATABASE_URL in your .env "
                f"(copy .env.example if you have not yet)."
            )
        return url

    def schema_for(self, side: str) -> str | None:
        return self.v2_schema if side == "v2" else self.v3_schema


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name)
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    def _opt(name: str) -> str | None:
        val = (os.getenv(name) or "").strip()
        return val or None

    return Settings(
        v2_url=(os.getenv("V2_DATABASE_URL") or "").strip(),
        v2_schema=_opt("V2_SCHEMA"),
        v3_url=(os.getenv("V3_DATABASE_URL") or "").strip(),
        v3_schema=_opt("V3_SCHEMA"),
        v3_api_base_url=_opt("V3_API_BASE_URL"),
        v3_api_token=_opt("V3_API_TOKEN"),
        v3_api_timeout=_int("V3_API_TIMEOUT", 30),
        batch_size=_int("BATCH_SIZE", 500),
        on_error=(os.getenv("ON_ERROR") or "record").strip().lower(),
        redact_patterns=(os.getenv("REDACT_PATTERNS") or DEFAULT_REDACT).split(","),
    )


def ensure_dirs() -> None:
    for d in (PROFILES_DIR, REPORTS_DIR, STATE_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)

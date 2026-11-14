"""Application settings, read from the environment (prefix ``ZP_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZP_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://postgres@127.0.0.1:5432/zephyr"

    # 32-byte key, base64 (urlsafe or standard). Encrypts secrets at rest (invariant I7).
    # Format "<key_id>:<base64>" or just "<base64>" (key id "k1"). Several keys separated by
    # commas: the first one encrypts, all of them decrypt (key rotation).
    secret_keys: SecretStr = Field(default=SecretStr(""))
    # Separate 32-byte key (base64) for stock fingerprints (HMAC of the login). Unlike the
    # encryption keys it must never change: duplicates are detected by comparing fingerprints.
    fingerprint_key: SecretStr = Field(default=SecretStr(""))

    # Sessions
    session_cookie: str = "zp_session"
    csrf_cookie: str = "zp_csrf"
    session_max_age_days: int = 30
    session_idle_days: int = 7
    cookie_secure: bool = True

    # Login rate limit (invariant I6)
    login_max_failures_per_email: int = 5
    login_max_failures_per_ip: int = 20
    login_window_minutes: int = 15

    # Jobs
    job_lock_timeout_seconds: int = 300
    worker_poll_seconds: float = 1.0

    # Serve the built panel (frontend/dist) from the API process when set.
    static_dir: str | None = None

    log_level: str = "INFO"

    @property
    def is_test(self) -> bool:
        return self.env == "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()

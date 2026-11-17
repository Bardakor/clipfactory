"""Process-level configuration: secrets and infrastructure, read from the environment.

Everything a user tunes at runtime (model routing, budgets, subtitle defaults…)
lives in SQLite instead — see ``settings_store``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipfactory.errors import ValidationFailed

REPO_ROOT = Path(__file__).resolve().parents[3]
RENDERER_DIR = REPO_ROOT / "apps" / "renderer"

DATA_SUBDIRS = (
    "sources",
    "audio",
    "transcripts",
    "events",
    "candidates",
    "previews",
    "exports",
    "cache",
    "work",
    "thumbnails",
    "live",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    clipfactory_data_dir: Path = REPO_ROOT / "data"
    clipfactory_api_port: int = 8000
    clipfactory_max_upload_gb: float = 30
    clipfactory_log_level: str = "INFO"
    clipfactory_ai_adapter: Literal["openrouter", "fake"] = "openrouter"
    clipfactory_transcriber: Literal["mlx", "openrouter", "fake"] | None = None

    @field_validator("openrouter_api_key", "clipfactory_transcriber", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("clipfactory_data_dir")
    @classmethod
    def _resolve_data_dir(cls, v: Path) -> Path:
        return (v if v.is_absolute() else REPO_ROOT / v).resolve()

    @property
    def data_dir(self) -> Path:
        return self.clipfactory_data_dir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipfactory.sqlite3"

    @property
    def has_openrouter_key(self) -> bool:
        return self.openrouter_api_key is not None

    @property
    def test_mode(self) -> bool:
        """True when any stage is served by a fake adapter rather than real inference."""
        return self.clipfactory_ai_adapter == "fake" or self.clipfactory_transcriber == "fake"

    def ensure_dirs(self) -> None:
        for sub in DATA_SUBDIRS:
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        """Resolve a path under the data dir, refusing anything that escapes it."""
        p = self.data_dir.joinpath(*parts).resolve()
        if p != self.data_dir and self.data_dir not in p.parents:
            raise ValidationFailed("path escapes the data directory", path=str(p))
        return p

    def rel(self, p: Path) -> str:
        return str(p.resolve().relative_to(self.data_dir))


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s

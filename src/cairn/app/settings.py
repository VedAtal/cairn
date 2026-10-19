from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class DBSettings(BaseModel):
    url: str | None = None
    # Non-pooled, for migrations: they need a session-level advisory lock.
    migrate_url: str | None = None


class RemoteSettings(BaseModel):
    url: str
    api_key: str
    alias: str


class Settings(BaseSettings):
    mode: Literal["hosted", "local"] = "hosted"
    home: Path = Path("~/.cairn")
    db: DBSettings = DBSettings()
    store_alias: str = "cairn"
    setup_token: str | None = None
    log_level: str = "INFO"
    json_logs: bool = False

    model_config = SettingsConfigDict(env_prefix="CAIRN_", env_nested_delimiter="__")

    @property
    def home_dir(self) -> Path:
        return self.home.expanduser()

    @property
    def alias(self) -> str:
        return "me" if self.mode == "local" else self.store_alias

    @property
    def database_url(self) -> str:
        if self.db.url:
            return self.db.url
        if self.mode == "local":
            return f"sqlite:///{self.home_dir / 'cairn.db'}"
        raise ValueError("CAIRN_DB__URL is required in hosted mode")

    @property
    def token_file(self) -> Path:
        return self.home_dir / "run" / "token"

    def remote(self) -> RemoteSettings | None:
        if self.mode != "local":
            return None
        config, creds = _toml(self.home_dir / "config.toml"), _toml(self.home_dir / "credentials.toml")
        remote, key = config.get("remote", {}), creds.get("remote", {}).get("api_key")
        if not (remote.get("url") and key):
            return None
        return RemoteSettings(url=remote["url"], api_key=key, alias=remote.get("alias", "remote"))

    def git(self) -> tuple[bool, str | None]:
        if self.mode != "local":
            return False, None
        found = _toml(self.home_dir / "config.toml").get("git", {})
        return bool(found.get("enabled")), found.get("remote") or None


def _toml(path: Path) -> dict[str, dict[str, str]]:
    return tomllib.loads(path.read_text()) if path.exists() else {}

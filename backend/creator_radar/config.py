import os
from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = Path("Scripts/python.exe" if os.name == "nt" else "bin/python")
DEFAULT_ASR_PYTHON = ROOT / ".phase1" / "asr-env" / VENV_PYTHON


def default_data_dir(workspace: str) -> Path:
    user_dir = user_data_path("CreatorRadar2", appauthor=False)
    legacy_dir = ROOT / ".creator-radar"
    if (user_dir / workspace / "workspace.sqlite3").is_file():
        return user_dir
    if (legacy_dir / workspace / "workspace.sqlite3").is_file():
        return legacy_dir
    return user_dir


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREATOR_RADAR_")

    workspace: Literal["production", "acceptance"] = "production"
    data_dir: Path = Field(default_factory=lambda data: default_data_dir(data["workspace"]))
    web_dist: Path = ROOT / "web" / "dist"
    port: int = Field(default=8765, ge=1, le=65535)
    dev_port: int | None = Field(default=None, ge=1, le=65535)
    crawler_runtime: Path = ROOT / ".phase1" / "MediaCrawler"
    crawler_setup_failed: bool = False
    crawler_fixture: bool = False
    crawler_fixture_media: Path | None = None
    asr_python: Path = DEFAULT_ASR_PYTHON

    @property
    def database_path(self) -> Path:
        return self.data_dir.resolve() / self.workspace / "workspace.sqlite3"

    @property
    def workspace_dir(self) -> Path:
        return self.database_path.parent

    @property
    def allowed_origins(self) -> set[str]:
        ports = {self.port} if self.dev_port is None else {self.port, self.dev_port}
        return {f"http://{host}:{port}" for port in ports for host in ("127.0.0.1", "localhost")}

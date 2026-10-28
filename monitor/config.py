"""Settings, read once from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _apps_file() -> Path:
    explicit = os.environ.get("STX_APPS_FILE")
    if explicit:
        return Path(explicit)
    # A mounted /config/apps.yaml wins over the copy bundled in the image.
    mounted = Path("/config/apps.yaml")
    if mounted.is_file():
        return mounted
    return REPO_DIR / "config" / "apps.yaml"


@dataclass(frozen=True)
class Settings:
    # Where the host's root filesystem is visible. "/host" in the container,
    # "/" when running straight on the server during development.
    host_root: Path
    # tcp://127.0.0.1:2375, unix:///var/run/docker.sock, or "none".
    docker_host: str
    apps_file: Path
    data_dir: Path
    bind: str
    port: int
    power_rate: float
    git_sha: str
    web_dir: Path
    # Optional first name for the greeting on the overview page.
    owner: str

    @property
    def docker_enabled(self) -> bool:
        return self.docker_host.lower() not in ("", "none", "off")


def load() -> Settings:
    return Settings(
        host_root=Path(os.environ.get("STX_HOST_ROOT", "/")),
        docker_host=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
        apps_file=_apps_file(),
        data_dir=Path(os.environ.get("STX_DATA_DIR", REPO_DIR / "data")),
        bind=os.environ.get("STX_BIND", "0.0.0.0"),
        port=_int("STX_PORT", 8090),
        power_rate=_float("STX_POWER_RATE", 0.15),
        git_sha=os.environ.get("STX_GIT_SHA", "dev"),
        web_dir=Path(os.environ.get("STX_WEB_DIR", REPO_DIR / "web")),
        owner=os.environ.get("STX_OWNER", ""),
    )


settings = load()

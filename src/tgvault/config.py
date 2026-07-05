"""Local config: Telegram app credentials. Stored owner-readable only."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from tgvault.paths import config_path, ensure_data_dir, restrict_file

API_ID_ENV = "TGVAULT_API_ID"
API_HASH_ENV = "TGVAULT_API_HASH"

# A packaged build may bundle app credentials so non-technical users never
# have to visit my.telegram.org. Not committed to git.
BUNDLED = Path(__file__).parent / "bundled_credentials.json"


@dataclass
class Credentials:
    api_id: int
    api_hash: str


def load_config() -> dict:
    path = config_path()
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_config(config: dict) -> None:
    ensure_data_dir()
    path = config_path()
    path.write_text(json.dumps(config, indent=2))
    restrict_file(path)


def load_credentials() -> Credentials | None:
    api_id = os.environ.get(API_ID_ENV)
    api_hash = os.environ.get(API_HASH_ENV)
    if api_id and api_hash:
        return Credentials(api_id=int(api_id), api_hash=api_hash)

    config = load_config()
    if config.get("api_id") and config.get("api_hash"):
        return Credentials(api_id=int(config["api_id"]), api_hash=config["api_hash"])

    if BUNDLED.exists():
        bundled = json.loads(BUNDLED.read_text())
        if bundled.get("api_id") and bundled.get("api_hash"):
            return Credentials(api_id=int(bundled["api_id"]), api_hash=bundled["api_hash"])

    from tgvault.defaults import DEFAULT_API_HASH, DEFAULT_API_ID

    return Credentials(api_id=DEFAULT_API_ID, api_hash=DEFAULT_API_HASH)


def save_credentials(api_id: int, api_hash: str) -> None:
    config = load_config()
    config["api_id"] = api_id
    config["api_hash"] = api_hash
    save_config(config)

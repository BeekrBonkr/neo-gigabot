from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Config:
    token: str
    owner_id: int
    default_prefix: str = "!"
    log_level: str = "INFO"



def load_config() -> Config:
    project_root = Path(__file__).resolve().parents[1]
    env_path = project_root / ".env"
    load_dotenv(env_path)

    token = os.getenv("DISCORD_TOKEN", "")
    owner_id = int(os.getenv("OWNER_ID", "0"))
    default_prefix = os.getenv("DEFAULT_PREFIX", "!")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Create gigabot/.env from gigabot/.env.example."
        )

    return Config(
        token=token,
        owner_id=owner_id,
        default_prefix=default_prefix,
        log_level=log_level,
    )

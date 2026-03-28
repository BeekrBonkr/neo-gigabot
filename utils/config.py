from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Config:
    token: str
    owner_id: int
    log_level: str = "INFO"
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "neo-gigabot/1.0"
    reddit_username: str = ""
    reddit_password: str = ""

    @property
    def reddit_enabled(self) -> bool:
        return all(
            [
                self.reddit_client_id.strip(),
                self.reddit_client_secret.strip(),
                self.reddit_user_agent.strip(),
                self.reddit_username.strip(),
                self.reddit_password.strip(),
            ]
        )


def load_config() -> Config:
    project_root = Path(__file__).resolve().parents[1]
    env_path = project_root / ".env"
    load_dotenv(env_path)

    token = os.getenv("DISCORD_TOKEN", "").strip()
    owner_id = int(os.getenv("OWNER_ID", "0"))
    log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Create neo-gigabot/.env from "
            "neo-gigabot/.env.example."
        )

    return Config(
        token=token,
        owner_id=owner_id,
        log_level=log_level,
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", "").strip(),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", "").strip(),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "neo-gigabot/1.0").strip()
        or "neo-gigabot/1.0",
        reddit_username=os.getenv("REDDIT_USERNAME", "").strip(),
        reddit_password=os.getenv("REDDIT_PASSWORD", "").strip(),
    )

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
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "neo-gigabot/1.0"
    reddit_username: str = ""
    reddit_password: str = ""
    openweathermap_api_key: str = ""
    giphy_api_key: str = ""
    support_server_invite: str = ""
    legacy_data_path: str = ""

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

    @property
    def weather_enabled(self) -> bool:
        return bool(self.openweathermap_api_key.strip())

    @property
    def giphy_enabled(self) -> bool:
        return bool(self.giphy_api_key.strip())



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
            f"DISCORD_TOKEN is missing. Create {env_path} from {project_root / '.env.example'}."
        )

    return Config(
        token=token,
        owner_id=owner_id,
        default_prefix=default_prefix,
        log_level=log_level,
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "neo-gigabot/1.0"),
        reddit_username=os.getenv("REDDIT_USERNAME", ""),
        reddit_password=os.getenv("REDDIT_PASSWORD", ""),
        openweathermap_api_key=os.getenv("OPENWEATHERMAP_API_KEY", ""),
        giphy_api_key=os.getenv("GIPHY_API_KEY", ""),
        support_server_invite=os.getenv("SUPPORT_SERVER_INVITE", ""),
        legacy_data_path=os.getenv("LEGACY_DATA_PATH", ""),
    )

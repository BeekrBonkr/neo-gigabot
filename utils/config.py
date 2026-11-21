from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Config:
    token: str
    owner_id: int
    log_level: str = "INFO"
    openweathermap_api_key: str = ""
    giphy_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = ""
    support_server_invite: str = ""
    website_url: str = ""
    legacy_data_path: str = ""
    beta: bool = False

    @property
    def weather_enabled(self) -> bool:
        return bool(self.openweathermap_api_key.strip())

    @property
    def giphy_enabled(self) -> bool:
        return bool(self.giphy_api_key.strip())

    @property
    def reddit_api_enabled(self) -> bool:
        return bool(self.reddit_client_id.strip() and self.reddit_client_secret.strip())



BETA_NOTICE = (
    "⚠️ **This is a beta test build.** Features can change or break without warning, "
    "and saved data may be reset."
)


def beta_notice(config: object) -> str:
    """The warning shown in /about and /help on a beta build; empty otherwise."""
    return BETA_NOTICE if getattr(config, "beta", False) else ""


def _http_url(value: str) -> str:
    """A configured link, without a trailing slash; anything that isn't http(s) is dropped."""
    value = (value or "").strip().rstrip("/")
    return value if value.startswith(("https://", "http://")) else ""


def load_config() -> Config:
    project_root = Path(__file__).resolve().parents[1]
    env_path = project_root / ".env"
    load_dotenv(env_path)

    token = os.getenv("DISCORD_TOKEN", "")
    owner_id = int(os.getenv("OWNER_ID", "0"))
    log_level = os.getenv("LOG_LEVEL", "INFO")

    if not token:
        raise RuntimeError(
            f"DISCORD_TOKEN is missing. Create {env_path} from {project_root / '.env.example'}."
        )

    return Config(
        token=token,
        owner_id=owner_id,
        log_level=log_level,
        openweathermap_api_key=os.getenv("OPENWEATHERMAP_API_KEY", ""),
        giphy_api_key=os.getenv("GIPHY_API_KEY", ""),
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", ""),
        support_server_invite=os.getenv("SUPPORT_SERVER_INVITE", ""),
        website_url=_http_url(os.getenv("WEBSITE_URL", "")),
        legacy_data_path=os.getenv("LEGACY_DATA_PATH", ""),
        beta=os.getenv("BETA_BUILD", "").strip() == "1",
    )

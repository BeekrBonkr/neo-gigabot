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
    support_server_invite: str = ""
    legacy_data_path: str = ""

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
        support_server_invite=os.getenv("SUPPORT_SERVER_INVITE", ""),
        legacy_data_path=os.getenv("LEGACY_DATA_PATH", ""),
    )

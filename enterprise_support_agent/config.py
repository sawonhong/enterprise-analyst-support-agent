"""Typed configuration management for the Enterprise Support Agent."""

import os
from pathlib import Path
from typing import Optional

from enterprise_support_agent.secret_manager import secret_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_PATH = str(PROJECT_ROOT / "data" / "customer_tickets.csv")
DEFAULT_SQLITE_PATH = str(PROJECT_ROOT / "data" / "support_state.db")
DEFAULT_MUTATIONS_JSON_PATH = str(PROJECT_ROOT / "data" / "ticket_mutations.json")

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore

    class AgentSettings(BaseSettings):
        """Application configuration backed by Pydantic Settings and Secret Manager."""

        model_config = SettingsConfigDict(
            env_file=str(PROJECT_ROOT / ".env"),
            env_file_encoding="utf-8",
            extra="ignore",
        )

        app_name: str = "enterprise_support_agent"
        google_api_key: Optional[str] = None
        gemini_api_key: Optional[str] = None
        router_model: str = "gemini-2.5-flash"
        synthesis_model: str = "gemini-2.5-flash"
        ticket_dataset_path: str = DEFAULT_DATASET_PATH
        sqlite_db_path: str = DEFAULT_SQLITE_PATH
        mutations_json_path: str = DEFAULT_MUTATIONS_JSON_PATH
        webhook_endpoint_url: str = (
            "https://api.enterprise-support.internal/v1/webhooks/tickets"
        )
        enable_telemetry: bool = True
        log_level: str = "INFO"

        def ensure_directories(self) -> None:
            """Create required runtime data directories if they do not exist."""
            Path(self.ticket_dataset_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.sqlite_db_path).parent.mkdir(parents=True, exist_ok=True)

        @property
        def active_api_key(self) -> Optional[str]:
            """Return the active Gemini/Google API key if configured."""
            key = self.google_api_key or self.gemini_api_key
            if key and key != "your_gemini_api_key_here":
                return key
            return None

    settings = AgentSettings()

except ImportError:

    class AgentSettingsFallback:
        """Standard-library fallback settings when pydantic-settings is not installed."""

        def __init__(self) -> None:
            self.app_name: str = os.getenv("APP_NAME", "enterprise_support_agent")
            self.google_api_key: Optional[str] = os.getenv("GOOGLE_API_KEY")
            self.gemini_api_key: Optional[str] = os.getenv("GEMINI_API_KEY")
            self.router_model: str = os.getenv("ROUTER_MODEL", "gemini-2.5-flash")
            self.synthesis_model: str = os.getenv("SYNTHESIS_MODEL", "gemini-2.5-flash")
            self.ticket_dataset_path: str = os.getenv(
                "TICKET_DATASET_PATH", DEFAULT_DATASET_PATH
            )
            self.sqlite_db_path: str = os.getenv("SQLITE_DB_PATH", DEFAULT_SQLITE_PATH)
            self.mutations_json_path: str = os.getenv(
                "MUTATIONS_JSON_PATH", DEFAULT_MUTATIONS_JSON_PATH
            )
            self.webhook_endpoint_url: str = os.getenv(
                "WEBHOOK_ENDPOINT_URL",
                "https://api.enterprise-support.internal/v1/webhooks/tickets",
            )
            self.enable_telemetry: bool = (
                os.getenv("ENABLE_TELEMETRY", "true").lower() == "true"
            )
            self.log_level: str = os.getenv("LOG_LEVEL", "INFO")

        def ensure_directories(self) -> None:
            """Create required runtime data directories if they do not exist."""
            Path(self.ticket_dataset_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.sqlite_db_path).parent.mkdir(parents=True, exist_ok=True)

        @property
        def active_api_key(self) -> Optional[str]:
            """Return the active Gemini/Google API key if configured."""
            key = self.google_api_key or self.gemini_api_key
            if key and key != "your_gemini_api_key_here":
                return key
            return None

    settings = AgentSettingsFallback()  # type: ignore

if not settings.active_api_key:
    resolved_secret = secret_client.get_secret(
        secret_id="enterprise-support-gemini-api-key",
        fallback_env="GOOGLE_API_KEY",
    ) or secret_client.get_secret(
        secret_id="enterprise-support-gemini-api-key",
        fallback_env="GEMINI_API_KEY",
    )
    if resolved_secret:
        settings.google_api_key = resolved_secret
        settings.gemini_api_key = resolved_secret

settings.ensure_directories()

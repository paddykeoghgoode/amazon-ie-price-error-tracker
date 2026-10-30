from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    twilio_whatsapp_to: str = ""

    poll_interval_seconds: int = 45
    request_delay_seconds: float = 2.0

    min_discount_percent: float = 75.0
    min_list_price_eur: float = 20.0
    max_alert_price_eur: float = 500.0
    cooldown_minutes: int = 30

    host: str = "0.0.0.0"
    port: int = 8080
    database_path: Path = Path("data/tracker.db")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(
            self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_whatsapp_from
            and self.twilio_whatsapp_to
        )


settings = Settings()

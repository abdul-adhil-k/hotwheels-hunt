from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    debug: bool = False

    # Scraper
    firstcry_search_url: str = "https://www.firstcry.com/search?q=hot+wheels"
    check_interval_minutes: int = 2
    check_interval_seconds: int = 0  # if >0 overrides check_interval_minutes
    max_pages: int = 0  # 0 = scrape the whole catalog (auto-detected page count)
    scrape_concurrency: int = 10  # concurrent page fetches (top-down + bottom-up)

    # Pincode
    default_pincode: str = "400001"

    # Email
    email_enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""

    # Telegram
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # WhatsApp via Twilio
    whatsapp_enabled: bool = False
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    whatsapp_to: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

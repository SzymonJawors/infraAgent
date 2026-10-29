from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration.

    AWS keys are for the Terraform process only. The Gemini key is for the
    chat model. Neither belongs in prompts, ``.tf`` files, or logs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "eu-central-1"
    google_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "google_api_key",
        ),
    )
    gemini_model: str = "gemini-4-argon"


def get_settings() -> Settings:
    return Settings.settings_cached()

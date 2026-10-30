from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration.

    AWS keys are for the Terraform process only. The OpenAI key is for the
    chat model. Neither belongs in prompts, ``.tf`` files, or logs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "eu-central-1"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"


def get_settings() -> Settings:
    return Settings.settings_cached()

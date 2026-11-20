from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.contracts import CameraConfig, Limits, PublicConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="READER_",
        env_nested_delimiter="__",
        extra="ignore",
        populate_by_name=True,
        env_ignore_empty=True,
    )

    mode: Literal["local", "aws"] = "local"
    aws_region: str = "ap-southeast-1"
    table_name: str = ""
    media_bucket: str = ""
    queue_url: str = ""
    secret_arn: str = ""
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("READER_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openrouter_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("READER_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    )
    vision_model: str = "google/gemini-3.5-flash-lite"
    speech_provider: Literal["openrouter", "openai"] = "openrouter"
    speech_model: str = "google/gemini-3.8-flash-tts"
    speech_voice: str = "Aoede"
    speech_chunk_characters: int = Field(default=1000, ge=100, le=4000)
    speech_pcm_rate: int = Field(default=24000, ge=8000, le=48000)
    provider_timeout_seconds: float = Field(default=60, ge=1, le=120)
    camera: CameraConfig = CameraConfig()
    limits: Limits = Limits()

    def public_config(self) -> PublicConfig:
        vision = bool(self.openrouter_api_key.get_secret_value() and self.vision_model)
        speech = bool(
            self.speech_provider == "openrouter"
            and self.openrouter_api_key.get_secret_value()
            and self.speech_model
            and self.speech_voice
        )
        missing = []
        if not vision:
            missing.append("OpenRouter key and vision model")
        if not speech:
            missing.append(
                "OpenRouter key, speech model and voice (OpenAI Realtime is not wired yet)"
            )
        if self.mode == "aws":
            configured = bool(
                self.table_name and self.media_bucket and self.queue_url and self.secret_arn
            )
            vision = speech = configured
            missing = [] if configured else ["AWS reading resources"]
        return PublicConfig(
            mode=self.mode,
            camera=self.camera,
            limits=self.limits,
            recognition_available=vision,
            speech_available=speech,
            setup_message=(
                "Configure server-only " + "; ".join(missing) + ", then restart npm run dev."
            )
            if missing
            else None,
        )


class FileSettings(Settings):
    model_config = SettingsConfigDict(env_file=(".envrc", ".env"))

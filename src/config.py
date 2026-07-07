import json
from typing import Any

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource


class _CommaSplitEnvSource(EnvSettingsSource):
    def prepare_field_value(self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool) -> Any:
        if isinstance(value, str) and self.field_is_complex(field):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return value  # return raw string; field_validator handles it
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    bot_token: str
    admin_tg_ids: list[int]
    nd_url: str
    nd_admin_user: str
    nd_admin_pass: str
    nd_music_path: str

    music_root: str = "/music"
    staging_root: str = "/staging"
    mb_search_limit: int = 48

    # Own AcoustID app key for fingerprint lookups. Unset -> chroma's built-in
    # globally-shared key (works, but rate-limited under contention). Register a
    # free key at https://acoustid.org/ to get a private budget.
    acoustid_api_key: str | None = None

    # Self-hosted Telegram Bot API server. Code default is the cloud API (20 MB
    # download cap): url unset, local off. Set BOT_API_URL to a local server to
    # lift the cap to 2000 MB, and BOT_API_LOCAL=1 if it runs with --local.
    bot_api_url: str | None = None
    bot_api_local: bool = False

    @field_validator("admin_tg_ids", mode="before")
    @classmethod
    def parse_ids(cls, v: Any) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @classmethod
    def settings_customise_sources(cls, settings_cls, env_settings, dotenv_settings, init_settings, **kwargs):
        return (init_settings, _CommaSplitEnvSource(settings_cls), dotenv_settings)


class _LazySettings:
    _instance: "Settings | None" = None

    def __getattr__(self, name: str) -> Any:
        if self._instance is None:
            self._instance = Settings()
        return getattr(self._instance, name)


settings: Settings = _LazySettings()  # type: ignore[assignment]

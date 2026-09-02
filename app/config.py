from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.yaml"


class MatcherThresholdsConfig(BaseModel):
    accept_threshold: float = 0.70
    review_threshold: float = 0.50


class TargetModelItem(BaseModel):
    manufacturer: str
    canonical_model: str = Field(default="")
    equipment_type: Optional[str] = "Heavy Equipment"
    aliases: List[str] = Field(default_factory=list)
    manufacturer_aliases: List[str] = Field(default_factory=list)

    @field_validator("canonical_model", mode="before")
    @classmethod
    def validate_canonical_model(cls, v, info):
        return v or ""

    @field_validator("aliases", mode="before")
    @classmethod
    def validate_aliases(cls, v, info):
        if not v:
            return []
        return list(v)


class TargetModelConfig(BaseModel):
    canonical_name: str
    aliases: List[str] = Field(default_factory=list)

    @field_validator("aliases", mode="before")
    @classmethod
    def ensure_aliases(cls, v, info):
        if not v:
            return []
        return v


class CategoryConfig(BaseModel):
    equipment_type: str
    target_models: List[TargetModelConfig] = Field(default_factory=list)

    @field_validator("target_models", mode="before")
    @classmethod
    def parse_target_models(cls, v):
        parsed: List[TargetModelConfig] = []
        for item in v:
            if isinstance(item, str):
                parsed.append(TargetModelConfig(canonical_name=item, aliases=[item]))
            elif isinstance(item, dict):
                canon = item.get("canonical_name")
                aliases = item.get("aliases", [canon])
                if canon not in aliases:
                    aliases = [canon] + aliases
                parsed.append(TargetModelConfig(canonical_name=canon, aliases=aliases))
            elif isinstance(item, TargetModelConfig):
                parsed.append(item)
        return parsed


class ManufacturerConfig(BaseModel):
    canonical_name: str
    aliases: List[str]
    categories: Dict[str, CategoryConfig] = Field(default_factory=dict)


class NegativeTermsConfig(BaseModel):
    parts_and_accessories: List[str] = Field(default_factory=list)
    documentation: List[str] = Field(default_factory=list)
    merchandise_and_toys: List[str] = Field(default_factory=list)

    @property
    def all_negative_terms(self) -> List[str]:
        return (
            self.parts_and_accessories
            + self.documentation
            + self.merchandise_and_toys
        )


class WatchlistConfig(BaseModel):
    matcher_thresholds: MatcherThresholdsConfig = Field(
        default_factory=MatcherThresholdsConfig
    )
    search_queries: List[str] = Field(default_factory=list)
    target_models: List[TargetModelItem] = Field(default_factory=list)
    manufacturers: Dict[str, ManufacturerConfig] = Field(default_factory=dict)
    equipment_terms: List[str] = Field(default_factory=list)
    negative_terms: NegativeTermsConfig = Field(default_factory=NegativeTermsConfig)
    positive_context_indicators: List[str] = Field(default_factory=list)


def load_watchlist(path: Optional[Path] = None) -> WatchlistConfig:
    target_path = path or WATCHLIST_PATH
    if not target_path.exists():
        raise FileNotFoundError(f"Watchlist configuration not found at {target_path}")

    with open(target_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return WatchlistConfig(**data)


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    MONITOR_INTERVAL_SECONDS: int = 120

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/equipment_monitor"
    REDIS_URL: str = "redis://localhost:6379/0"

    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    MATCH_ACCEPT_THRESHOLD: float = 0.70
    MATCH_REVIEW_THRESHOLD: float = 0.50

    MACHINERYTRADER_ENABLED: bool = True
    MACHINIO_ENABLED: bool = True
    FACEBOOK_MARKETPLACE_ENABLED: bool = False
    FACEBOOK_PROVIDER_API_KEY: Optional[str] = None
    FACEBOOK_PROVIDER_ENDPOINT: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

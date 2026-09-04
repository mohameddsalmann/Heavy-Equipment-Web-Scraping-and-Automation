from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, Field

from app.config import FACEBOOK_REGIONS_PATH


class FacebookRegion(BaseModel):
    """Configuration for a Facebook Marketplace search region."""

    id: str
    name: str
    country: str = "US"  # "US" or "CA"
    location_id: str
    search_url: str
    enabled: bool = True
    priority: int = 1


class FacebookRegionsConfig(BaseModel):
    regions: List[FacebookRegion] = Field(default_factory=list)


def load_facebook_regions(path: Optional[Path] = None) -> List[FacebookRegion]:
    """
    Load and parse the Facebook Marketplace region grid from YAML.
    Returns only enabled regions sorted by priority.
    """
    target_path = path or FACEBOOK_REGIONS_PATH
    if not target_path.exists():
        return []

    with open(target_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    config = FacebookRegionsConfig(**data)
    enabled_regions = [r for r in config.regions if r.enabled]
    enabled_regions.sort(key=lambda r: r.priority)
    return enabled_regions

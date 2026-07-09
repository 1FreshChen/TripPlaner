from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class UnsplashService:
    """Unsplash 图片服务。Key 为空时保持静默，不影响主流程。"""

    def __init__(self, access_key: str):
        self.access_key = access_key
        self.base_url = "https://api.unsplash.com"

    @property
    def enabled(self) -> bool:
        return bool(self.access_key)

    def search_photos(self, query: str, per_page: int = 10) -> List[Dict]:
        if not self.enabled:
            return []
        try:
            response = requests.get(
                f"{self.base_url}/search/photos",
                params={"query": query, "per_page": per_page, "client_id": self.access_key},
                timeout=10,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            return [
                {
                    "url": result["urls"]["regular"],
                    "description": result.get("description") or result.get("alt_description") or "",
                    "photographer": result["user"]["name"],
                }
                for result in results
            ]
        except Exception as exc:
            logger.warning("Unsplash photo search failed: %s", exc)
            return []

    def get_photo_url(self, query: str) -> Optional[str]:
        photos = self.search_photos(query, per_page=1)
        return photos[0]["url"] if photos else None

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from difflib import SequenceMatcher

from app.models.schemas import Attraction, TripPlan
from app.services.amap_service import AmapService, ServiceResult
from app.services.unsplash_service import UnsplashService


logger = logging.getLogger(__name__)

_AMAP_SOURCE_PREFIX = "amap"
_GENERIC_NAME_SUFFIXES = (
    "国家级风景名胜区",
    "风景名胜区",
    "旅游度假区",
    "旅游景区",
    "风景区",
    "公园",
    "景区",
)


class AttractionVerificationError(RuntimeError):
    """Raised when a planned attraction cannot be bound to a real AMap POI."""


def _normalized_name(value: str) -> str:
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def _core_name(value: str) -> str:
    normalized = _normalized_name(value)
    changed = True
    while changed and len(normalized) >= 3:
        changed = False
        for suffix in _GENERIC_NAME_SUFFIXES:
            normalized_suffix = _normalized_name(suffix)
            if normalized.endswith(normalized_suffix) and len(normalized) > len(normalized_suffix) + 1:
                normalized = normalized[: -len(normalized_suffix)]
                changed = True
                break
    return normalized


def attraction_name_similarity(left: str, right: str) -> float:
    """Score two POI names while tolerating common scenic-area suffixes."""
    left_name = _normalized_name(left)
    right_name = _normalized_name(right)
    if not left_name or not right_name:
        return 0.0
    if left_name == right_name:
        return 1.0

    left_core = _core_name(left)
    right_core = _core_name(right)
    if left_core and left_core == right_core:
        return 0.98
    if (
        min(len(left_core), len(right_core)) >= 2
        and abs(len(left_core) - len(right_core)) <= 2
        and (
        left_core in right_core or right_core in left_core
        )
    ):
        return 0.92

    return max(
        SequenceMatcher(None, left_name, right_name).ratio(),
        SequenceMatcher(None, left_core, right_core).ratio(),
    )


def _is_real_amap_attraction(attraction: Attraction) -> bool:
    return bool(
        attraction.poi_id
        and attraction.location
        and str(attraction.data_source or "").startswith(_AMAP_SOURCE_PREFIX)
    )


def _candidate_identity(attraction: Attraction) -> str:
    if attraction.poi_id:
        return f"poi:{attraction.poi_id}"
    return f"name:{_normalized_name(attraction.name)}|{_normalized_name(attraction.address)}"


def _best_candidate(
    planned: Attraction,
    candidates: Iterable[Attraction],
    used_poi_ids: set[str],
    *,
    threshold: float = 0.68,
) -> Attraction | None:
    best: Attraction | None = None
    best_score = 0.0
    planned_address = _normalized_name(planned.address)
    for candidate in candidates:
        if not _is_real_amap_attraction(candidate) or candidate.poi_id in used_poi_ids:
            continue
        name_score = attraction_name_similarity(planned.name, candidate.name)
        address_score = (
            SequenceMatcher(None, planned_address, _normalized_name(candidate.address)).ratio()
            if planned_address and candidate.address
            else 0.0
        )
        score = name_score if name_score >= 0.9 else name_score * 0.9 + address_score * 0.1
        if score > best_score:
            best = candidate
            best_score = score
    return best if best_score >= threshold else None


def _merge_candidates(*groups: Iterable[Attraction]) -> list[Attraction]:
    merged: list[Attraction] = []
    seen: set[str] = set()
    for group in groups:
        for attraction in group:
            if not _is_real_amap_attraction(attraction):
                continue
            identity = _candidate_identity(attraction)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(attraction)
    return merged


def _convert_search_result(
    result: ServiceResult[dict] | list[dict] | None,
    amap_service: AmapService,
    preferences: str,
) -> list[Attraction]:
    if result is None:
        return []
    if isinstance(result, ServiceResult):
        if result.is_error:
            detail = result.error or "unknown AMap provider error"
            raise AttractionVerificationError(
                f"高德景点搜索失败：source={result.source}, kind={result.error_kind}, error={detail}"
            )
        pois = result.data
    else:
        pois = result
    return [
        attraction
        for attraction in (amap_service.poi_to_attraction(poi, preferences) for poi in pois)
        if attraction is not None and _is_real_amap_attraction(attraction)
    ]


async def _search_amap_candidates(
    amap_service: AmapService,
    name: str,
    city: str,
    preferences: str,
) -> list[Attraction]:
    try:
        result = await asyncio.to_thread(amap_service.search_pois, name, city, 10)
    except Exception as exc:
        raise AttractionVerificationError(
            f"高德景点搜索异常：query={name}, error_type={type(exc).__name__}, error={exc}"
        ) from exc
    return _convert_search_result(result, amap_service, preferences)


def _apply_verified_poi(planned: Attraction, verified: Attraction) -> None:
    """Replace factual POI fields with the authoritative AMap record."""
    planned.name = verified.name
    planned.address = verified.address
    planned.location = verified.location.model_copy(deep=True)
    planned.category = verified.category
    planned.rating = verified.rating
    planned.poi_id = verified.poi_id
    planned.data_source = verified.data_source
    planned.coordinate_verified = True
    planned.ticket_price = verified.ticket_price
    planned.image_url = verified.image_url
    planned.image_source = (verified.image_source or "amap") if verified.image_url else None


async def enrich_attraction_images(
    plan: TripPlan,
    source_attractions: Iterable[Attraction],
    unsplash_service: UnsplashService,
    *,
    amap_service: AmapService | None = None,
    preferences: str = "",
    require_verified: bool = False,
    concurrency: int = 3,
) -> TripPlan:
    """Bind planned attractions to AMap POIs, then fill best-effort images.

    In strict mode every final attraction must resolve to a real AMap POI. The
    model may choose and order places, but cannot remain the source of POI
    identity, address, coordinates, category, rating, ticket value or image.
    """
    enriched = plan.model_copy(deep=True)
    candidates = _merge_candidates(source_attractions)
    used_poi_ids: set[str] = set()
    missing: dict[str, list[Attraction]] = {}

    for day in enriched.days:
        for attraction in day.attractions:
            original_name = attraction.name
            known = _best_candidate(attraction, candidates, used_poi_ids)
            if known is None and require_verified:
                if amap_service is None or not amap_service.enabled:
                    raise AttractionVerificationError(
                        f"景点“{original_name}”无法核验：高德服务未配置"
                    )
                searched = await _search_amap_candidates(
                    amap_service,
                    original_name,
                    enriched.city,
                    preferences,
                )
                candidates = _merge_candidates(candidates, searched)
                known = _best_candidate(attraction, searched, used_poi_ids)

            if known is not None:
                _apply_verified_poi(attraction, known)
                used_poi_ids.add(str(known.poi_id))
            elif require_verified:
                raise AttractionVerificationError(
                    f"景点“{original_name}”未在高德搜索结果中找到足够相似的真实 POI"
                )
            elif attraction.data_source != "mock":
                attraction.data_source = "llm"
                attraction.coordinate_verified = False

            normalized = _normalized_name(attraction.name)
            if attraction.image_url:
                attraction.image_source = attraction.image_source or "llm"
                continue
            if normalized:
                missing.setdefault(normalized, []).append(attraction)

    if not missing or not unsplash_service.enabled:
        return enriched

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def fetch(normalized: str, attractions: list[Attraction]) -> tuple[str, str | None]:
        async with semaphore:
            query = f"{enriched.city} {attractions[0].name}"
            try:
                url = await asyncio.to_thread(unsplash_service.get_photo_url, query)
            except Exception as exc:
                logger.warning("Attraction image lookup failed for '%s': %s", query, exc)
                url = None
            return normalized, url

    results = await asyncio.gather(
        *(fetch(normalized, attractions) for normalized, attractions in missing.items())
    )
    for normalized, url in results:
        if not url:
            continue
        for attraction in missing[normalized]:
            attraction.image_url = url
            attraction.image_source = "unsplash"
    return enriched

"""Utility helpers for region locking and playlist filtering."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

REGION_HEADER_CANDIDATES = [
    'X-Amps-Region',
    'X-Region',
    'CF-IPCountry',
    'X-Appengine-Country',
]


def _normalize_region_code(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    candidate = value.strip().upper()
    if not candidate:
        return None

    # Basic sanity check – ISO-3166 alpha-2 style codes only
    if len(candidate) == 2 and candidate.isalpha():
        return candidate

    return None


def extract_region_from_request(req) -> Optional[str]:  # type: ignore[valid-type]
    """Best-effort extraction of a client region from query parameters or headers."""

    region = _normalize_region_code(req.args.get('region'))
    if region:
        return region

    for header in REGION_HEADER_CANDIDATES:
        header_value = req.headers.get(header)
        normalized = _normalize_region_code(header_value)
        if normalized:
            return normalized

    return None


def _normalise_regions(regions: Optional[Iterable[str]]) -> List[str]:
    if not regions:
        return []
    normalised = []
    for region in regions:
        code = _normalize_region_code(region)
        if code:
            normalised.append(code)
    return normalised


def is_stream_allowed_for_region(stream: Dict, region: Optional[str]) -> bool:
    """Determines whether the client region is authorised to view the stream."""

    allow_list = _normalise_regions(stream.get('regions_allowed'))
    block_list = _normalise_regions(stream.get('regions_blocked'))

    if allow_list and (not region or region not in allow_list):
        return False

    if block_list and region and region in block_list:
        return False

    return True


def parse_group_filter(raw_value: Optional[str]) -> Optional[Set[str]]:
    if not raw_value:
        return None

    groups = {item.strip().lower() for item in raw_value.split(',') if item.strip()}
    return groups or None


def parse_id_filter(raw_value: Optional[str]) -> Optional[Set[int]]:
    if not raw_value:
        return None

    ids: Set[int] = set()
    for chunk in raw_value.split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue

    return ids or None


def filter_streams(
    streams: Iterable[Dict],
    region: Optional[str] = None,
    groups: Optional[Set[str]] = None,
    stream_ids: Optional[Set[int]] = None,
):
    """Yields streams that satisfy the provided region/group/id filters."""

    for stream in streams:
        if not isinstance(stream, dict):
            continue

        if stream_ids is not None and stream.get('id') not in stream_ids:
            continue

        if groups is not None:
            stream_group = (stream.get('group') or '').strip().lower()
            if stream_group not in groups:
                continue

        if not is_stream_allowed_for_region(stream, region):
            continue

        yield stream

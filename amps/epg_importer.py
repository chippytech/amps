"""XMLTV ingestion and scheduled EPG refresh helpers."""

from __future__ import annotations

import copy
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET


EPG_REFRESH_STATUS: Dict[str, Dict[str, Any]] = {}


def _read_source(source: Dict[str, Any]) -> bytes:
    if source.get('url'):
        with urllib.request.urlopen(source['url'], timeout=20) as response:  # pragma: no cover - network dependent
            return response.read()
    if source.get('path'):
        return Path(source['path']).read_bytes()
    raise ValueError('EPG source requires either url or path.')


def _parse_xmltv_time(raw_value: Optional[str]) -> Optional[datetime]:
    if not raw_value:
        return None
    candidate = raw_value.strip()
    for fmt in ('%Y%m%d%H%M%S %z', '%Y%m%d%H%M%S%z'):
        try:
            return datetime.strptime(candidate, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _extract_programmes(xml_bytes: bytes) -> Dict[str, List[Dict[str, Any]]]:
    root = ET.fromstring(xml_bytes)
    programmes: Dict[str, List[Dict[str, Any]]] = {}
    now = datetime.now(timezone.utc)

    for programme in root.findall('programme'):
        channel = programme.attrib.get('channel')
        if not channel:
            continue
        start = _parse_xmltv_time(programme.attrib.get('start'))
        end = _parse_xmltv_time(programme.attrib.get('stop'))
        if start and end and end < now:
            continue
        entry = {
            'title': (programme.findtext('title') or '').strip() or channel,
            'start': start.isoformat().replace('+00:00', 'Z') if start else None,
            'end': end.isoformat().replace('+00:00', 'Z') if end else None,
            'description': (programme.findtext('desc') or '').strip() or None,
        }
        programmes.setdefault(channel, []).append({k: v for k, v in entry.items() if v is not None})

    for channel, items in programmes.items():
        items.sort(key=lambda item: item.get('start', ''))
    return programmes


def refresh_epg_source(config: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    source_name = source.get('name') or source.get('url') or source.get('path') or 'epg-source'
    try:
        xml_bytes = _read_source(source)
        programmes = _extract_programmes(xml_bytes)
    except Exception as exc:
        status = {
            'name': source_name,
            'ok': False,
            'error': str(exc),
            'updated_streams': 0,
            'programme_count': 0,
            'last_refresh': datetime.now(timezone.utc).isoformat(),
        }
        EPG_REFRESH_STATUS[source_name] = status
        raise

    match_on = source.get('match_on') or 'epg_id'
    updated_streams = 0
    programme_count = sum(len(items) for items in programmes.values())

    for stream in config.get('stream_map', {}).values():
        candidate_keys = []
        if match_on == 'tvg_name':
            candidate_keys.append(str(stream.get('tvg_name') or '').strip())
            candidate_keys.append(str(stream.get('name') or '').strip())
        else:
            candidate_keys.append(str(stream.get('epg_id') or stream.get('tvg_id') or stream.get('id')))
            candidate_keys.append(str(stream.get('tvg_name') or '').strip())

        matched = next((copy.deepcopy(programmes[key]) for key in candidate_keys if key and key in programmes), None)
        if matched is None:
            continue
        stream['next_programs'] = matched
        updated_streams += 1

    status = {
        'name': source_name,
        'ok': True,
        'updated_streams': updated_streams,
        'programme_count': programme_count,
        'last_refresh': datetime.now(timezone.utc).isoformat(),
        'match_on': match_on,
    }
    EPG_REFRESH_STATUS[source_name] = status
    logging.info('Refreshed EPG source %s: %s streams updated.', source_name, updated_streams)
    return status


def list_epg_status(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    statuses = []
    for source in config.get('epg_sources', []) or []:
        name = source.get('name') or source.get('url') or source.get('path') or 'epg-source'
        snapshot = dict(EPG_REFRESH_STATUS.get(name, {}))
        snapshot.setdefault('name', name)
        snapshot.setdefault('ok', False)
        snapshot.setdefault('match_on', source.get('match_on') or 'epg_id')
        snapshot.setdefault('refresh_interval_minutes', source.get('refresh_interval_minutes'))
        snapshot.setdefault('source', source.get('url') or source.get('path'))
        statuses.append(snapshot)
    return statuses

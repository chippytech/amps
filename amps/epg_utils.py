"""Helpers for generating XMLTV and JSON EPG payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Dict, Any, List, Optional
from xml.etree.ElementTree import Element, SubElement, tostring

ISO_FORMATS = ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S')


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        candidate = value.strip()
        if candidate.endswith('Z'):
            candidate = candidate[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            for fmt in ISO_FORMATS:
                try:
                    dt = datetime.strptime(candidate, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _format_xmltv_time(dt: datetime) -> str:
    return dt.strftime('%Y%m%d%H%M%S %z')


def build_xmltv(streams: Iterable[Dict]) -> bytes:
    tv = Element('tv', attrib={'source-info-name': 'Amps', 'generator-info-name': 'Amps'})

    for stream in streams:
        channel_id = str(stream.get('epg_id') or stream.get('tvg_id') or stream.get('id'))
        channel_el = SubElement(tv, 'channel', id=channel_id)
        display_name = stream.get('name') or channel_id
        SubElement(channel_el, 'display-name').text = display_name
        tvg_name = stream.get('tvg_name')
        if tvg_name:
            SubElement(channel_el, 'display-name').text = tvg_name
        logo = stream.get('logo')
        if logo:
            SubElement(channel_el, 'icon', attrib={'src': logo})
        stream_url = stream.get('_stream_url')
        if stream_url:
            SubElement(channel_el, 'url').text = stream_url

        for program in stream.get('next_programs') or []:
            start_dt = _parse_datetime(program.get('start'))
            if not start_dt:
                continue
            attrs = {
                'start': _format_xmltv_time(start_dt),
                'channel': channel_id,
            }
            end_dt = _parse_datetime(program.get('end'))
            if end_dt:
                attrs['stop'] = _format_xmltv_time(end_dt)

            programme_el = SubElement(tv, 'programme', attrib=attrs)
            title = program.get('title') or stream.get('name') or channel_id
            SubElement(programme_el, 'title').text = title
            if program.get('description'):
                SubElement(programme_el, 'desc').text = program['description']

    return tostring(tv, encoding='utf-8', xml_declaration=True)


def build_epg_payload(streams: Iterable[Dict]) -> List[Dict[str, Any]]:
    payload = []
    for stream in streams:
        payload.append({
            'id': stream.get('id'),
            'epg_id': stream.get('epg_id') or stream.get('tvg_id') or stream.get('id'),
            'name': stream.get('name'),
            'group': stream.get('group'),
            'logo': stream.get('logo'),
            'programs': stream.get('next_programs') or [],
        })
    return payload

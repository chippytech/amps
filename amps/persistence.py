"""Helpers for persisting runtime state and syncing config snapshots."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import yaml

STATE_FILENAME = 'runtime_state.json'


def get_data_root(config: Dict[str, Any]) -> Path:
    configured = config.get('data_root') or config.get('server', {}).get('data_root')
    root = Path(configured or './data')
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_state_path(config: Dict[str, Any]) -> Path:
    return get_data_root(config) / STATE_FILENAME


def export_runtime_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    stream_map = config.get('stream_map', {}) or {}
    return {
        'streams': list(sorted(stream_map.values(), key=lambda item: item.get('id', 0))),
        'scheduled_streams': copy.deepcopy(config.get('scheduled_streams', [])),
        'ffmpeg_profiles': copy.deepcopy(config.get('ffmpeg_profiles', {})),
        'plugins': copy.deepcopy(config.get('plugins', [])),
        'webhooks': copy.deepcopy(config.get('webhooks', [])),
        'epg_sources': copy.deepcopy(config.get('epg_sources', [])),
        'auth': {
            'enabled': config.get('auth', {}).get('enabled', True),
            'token': config.get('auth', {}).get('token'),
            'api_keys': copy.deepcopy(config.get('auth', {}).get('api_keys', [])),
        },
        'server': copy.deepcopy(config.get('server', {})),
        'data_root': config.get('data_root'),
    }


def persist_runtime_state(config: Dict[str, Any]) -> Path:
    path = get_state_path(config)
    path.write_text(json.dumps(export_runtime_snapshot(config), indent=2, sort_keys=True), encoding='utf-8')
    return path


def load_persisted_state(config: Dict[str, Any]) -> Dict[str, Any]:
    path = get_state_path(config)
    if not path.exists():
        return config

    state = json.loads(path.read_text(encoding='utf-8'))
    merged = copy.deepcopy(config)
    for key in ['streams', 'scheduled_streams', 'ffmpeg_profiles', 'plugins', 'webhooks', 'epg_sources', 'data_root']:
        if key in state:
            merged[key] = state[key]
    if 'auth' in state and isinstance(state['auth'], dict):
        merged.setdefault('auth', {}).update(state['auth'])
    if 'server' in state and isinstance(state['server'], dict):
        merged.setdefault('server', {}).update(state['server'])
    return merged


def write_config_snapshot(config: Dict[str, Any], config_path: str) -> Path:
    payload = export_runtime_snapshot(config)
    path = Path(config_path)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding='utf-8')
    return path

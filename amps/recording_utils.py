"""Helpers for recording, listing, and clipping stream archives."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from amps import ffmpeg_utils

RECORDING_JOBS: Dict[int, Dict[str, Any]] = {}


def recordings_root(config: Dict[str, Any]) -> Path:
    root = Path(config.get('recordings_root') or Path(config.get('data_root') or './data') / 'recordings')
    root.mkdir(parents=True, exist_ok=True)
    return root


def _recording_dir(config: Dict[str, Any], stream_id: int) -> Path:
    path = recordings_root(config) / str(stream_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _recording_metadata(path: Path) -> Dict[str, Any]:
    return {
        'file': path.name,
        'path': str(path),
        'size_bytes': path.stat().st_size,
        'modified_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(path.stat().st_mtime)),
    }


def list_recordings(config: Dict[str, Any], stream_id: Optional[int] = None) -> List[Dict[str, Any]]:
    root = recordings_root(config)
    files: List[Dict[str, Any]] = []
    candidates = [root / str(stream_id)] if stream_id is not None else [p for p in root.iterdir() if p.is_dir()]
    for directory in candidates:
        if not directory.exists():
            continue
        sid = int(directory.name)
        for path in sorted(directory.glob('*'), reverse=True):
            if path.is_file():
                item = _recording_metadata(path)
                item['stream_id'] = sid
                files.append(item)
    return files


def start_recording(stream_config: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    stream_id = stream_config['id']
    existing = RECORDING_JOBS.get(stream_id)
    if existing and existing['process'].poll() is None:
        return existing['metadata']

    source, handler_options = ffmpeg_utils._resolve_stream_source(stream_config)  # type: ignore[attr-defined]
    if not source:
        raise RuntimeError('Unable to resolve stream source for recording.')

    target_dir = _recording_dir(config, stream_id)
    timestamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    output_path = target_dir / f'{timestamp}.mkv'

    command = ['ffmpeg', '-y']
    headers = handler_options.get('headers')
    if headers:
        command.extend(['-headers', headers])
    command.extend(['-i', source, '-c', 'copy', str(output_path)])

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    metadata = {
        'stream_id': stream_id,
        'path': str(output_path),
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'pid': process.pid,
    }
    RECORDING_JOBS[stream_id] = {
        'process': process,
        'metadata': metadata,
    }
    logging.info('Started recording for stream %s -> %s', stream_id, output_path)
    return metadata


def stop_recording(stream_id: int) -> Optional[Dict[str, Any]]:
    job = RECORDING_JOBS.get(stream_id)
    if not job:
        return None
    process = job['process']
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    metadata = dict(job['metadata'])
    metadata['stopped_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    RECORDING_JOBS.pop(stream_id, None)
    return metadata


def recording_status(stream_id: int) -> Dict[str, Any]:
    job = RECORDING_JOBS.get(stream_id)
    if not job:
        return {'active': False}
    process = job['process']
    return {
        'active': process.poll() is None,
        **job['metadata'],
    }


def export_clip(config: Dict[str, Any], stream_id: int, recording_file: str, clip_start: str, clip_end: str) -> Dict[str, Any]:
    source_path = _recording_dir(config, stream_id) / recording_file
    if not source_path.exists():
        raise FileNotFoundError(f'Recording {recording_file} not found for stream {stream_id}.')

    clip_name = f'clip_{Path(recording_file).stem}_{clip_start.replace(":", "-")}_{clip_end.replace(":", "-")}.mkv'
    clip_path = _recording_dir(config, stream_id) / clip_name
    command = [
        'ffmpeg', '-y', '-i', str(source_path), '-ss', clip_start, '-to', clip_end, '-c', 'copy', str(clip_path)
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or 'ffmpeg clip export failed')
    return _recording_metadata(clip_path) | {'stream_id': stream_id}

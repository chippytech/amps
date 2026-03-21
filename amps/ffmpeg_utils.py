# amps/ffmpeg_utils.py

from __future__ import annotations

import atexit
import ffmpeg
import logging
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    import yt_dlp
except ImportError:  # pragma: no cover - dependency should be installed, but guard just in case
    yt_dlp = None

# Global dictionary to hold running FFmpeg processes and associated data
# Structure: { (stream_id, variant): {'process': Popen_object, 'lock': Lock_object} }
RUNNING_PROCESSES: Dict[Tuple[int, str], Dict[str, Any]] = {}
PROCESS_STATS: Dict[Tuple[int, str], Dict[str, Any]] = {}
EVENT_LISTENER: Optional[Callable[[str, Dict[str, Any]], None]] = None
DEFAULT_VARIANT_KEY = 'default'
OUTPUT_BASE = Path(tempfile.gettempdir()) / 'amps_media'
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


def set_event_listener(listener: Optional[Callable[[str, Dict[str, Any]], None]]):
    global EVENT_LISTENER
    EVENT_LISTENER = listener


def _emit_event(event: str, payload: Dict[str, Any]):
    if EVENT_LISTENER:
        try:
            EVENT_LISTENER(event, payload)
        except Exception as exc:  # pragma: no cover - defensive only
            logging.error('Event listener failure for %s: %s', event, exc)


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _default_stats(stream_id: int, variant_key: str) -> Dict[str, Any]:
    return {
        'stream_id': stream_id,
        'variant': variant_key,
        'starts': 0,
        'restart_count': 0,
        'last_start_at': None,
        'last_stop_at': None,
        'last_exit_code': None,
        'last_error': None,
        'last_stderr': None,
        'last_source': None,
    }


def _stats_for(key: Tuple[int, str]) -> Dict[str, Any]:
    if key not in PROCESS_STATS:
        PROCESS_STATS[key] = _default_stats(key[0], key[1])
    return PROCESS_STATS[key]


def _resolve_stream_source(stream_config: dict) -> Tuple[Optional[str], Dict[str, Any]]:
    """Resolves the input source for FFmpeg, optionally using yt-dlp."""

    source = stream_config.get('source')
    if not source:
        logging.error("Stream '%s' is missing a source URL.", stream_config.get('name', stream_config.get('id')))
        return None, {}

    handler_conf = stream_config.get('source_handler') or {}

    if stream_config.get('use_yt_dlp') and not handler_conf:
        handler_conf = {
            'type': 'yt_dlp',
            'format': stream_config.get('yt_dlp_format')
        }

    handler_type = (handler_conf.get('type') or '').lower()

    if handler_type != 'yt_dlp':
        return source, {}

    if yt_dlp is None:
        logging.error(
            "yt-dlp support requested for stream '%s', but the package is not available.",
            stream_config.get('name', stream_config.get('id')),
        )
        return None, {}

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': handler_conf.get('format') or 'best',
        'noplaylist': True,
        'cachedir': False,
        'skip_download': True,
    }

    extra_opts = handler_conf.get('options')
    if isinstance(extra_opts, dict):
        ydl_opts.update(extra_opts)

    stream_label = stream_config.get('name', stream_config.get('id'))

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source, download=False)
    except Exception as exc:  # pragma: no cover - network errors/environment specific
        logging.error("yt-dlp failed to extract stream for '%s': %s", stream_label, exc)
        return None, {}

    if not info:
        logging.error("yt-dlp did not return stream information for '%s'.", stream_label)
        return None, {}

    if 'entries' in info:
        entries = info.get('entries') or []
        info = next((entry for entry in entries if entry), None)
        if not info:
            logging.error("yt-dlp returned an empty playlist for '%s'.", stream_label)
            return None, {}

    resolved = info.get('url') or info.get('manifest_url')
    if not resolved:
        logging.error("yt-dlp did not provide a playable URL for '%s'.", stream_label)
        return None, {}

    input_overrides: Dict[str, Any] = {}

    headers = info.get('http_headers')
    if headers:
        header_lines = ''.join(f"{key}: {value}\r\n" for key, value in headers.items())
        input_overrides['headers'] = header_lines

    protocol = info.get('protocol')
    if protocol and protocol.startswith('m3u8'):
        input_overrides.setdefault('protocol_whitelist', 'file,http,https,tcp,tls,crypto')

    return resolved, input_overrides


def _prepare_custom_ffmpeg_command(stream_config: dict) -> Optional[Tuple[Union[str, List[str]], bool, Optional[dict], Optional[str]]]:
    custom_conf = stream_config.get('custom_ffmpeg')
    if not custom_conf:
        return None

    if isinstance(custom_conf, str):
        custom_conf = {'command': custom_conf}

    if not isinstance(custom_conf, dict):
        logging.error('custom_ffmpeg configuration must be a string or mapping.')
        return None

    command_template = custom_conf.get('command')
    if not command_template:
        logging.error("custom_ffmpeg configuration missing 'command' entry.")
        return None

    context = {
        'source': stream_config.get('source', ''),
        'id': stream_config.get('id'),
        'name': stream_config.get('name', ''),
    }

    shell = bool(custom_conf.get('shell', False))

    if isinstance(command_template, list):
        command = [str(arg).format(**context) for arg in command_template]
    elif isinstance(command_template, str):
        formatted = command_template.format(**context)
        command = formatted if shell else shlex.split(formatted)
    else:
        logging.error("custom_ffmpeg 'command' must be a string or list of arguments.")
        return None

    env = custom_conf.get('env')
    cwd = custom_conf.get('cwd')

    if env is not None and not isinstance(env, dict):
        logging.error("custom_ffmpeg 'env' must be a mapping of environment variables.")
        env = None

    return command, shell, env, cwd


def _build_output_path(stream_id: int, variant_key: str, filename: str) -> Path:
    target_dir = OUTPUT_BASE / str(stream_id) / variant_key
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / filename


def _clean_output_path(path: Path):
    if path.exists():
        try:
            if path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError:
            logging.debug('Failed to clean previous output at %s', path)


def _apply_hwaccel(input_stream, hwaccel_conf: Optional[dict]):
    if not hwaccel_conf:
        return input_stream, []

    hw_type = hwaccel_conf.get('type')
    if not hw_type:
        return input_stream, []

    extra_global_args = []
    if hw_type == 'nvidia':
        extra_global_args.extend(['-hwaccel', 'cuda'])
    elif hw_type == 'vaapi':
        extra_global_args.extend(['-hwaccel', 'vaapi'])
    elif hw_type == 'videotoolbox':
        extra_global_args.extend(['-hwaccel', 'videotoolbox'])

    device = hwaccel_conf.get('device')
    if device:
        extra_global_args.extend(['-hwaccel_device', str(device)])

    return input_stream, extra_global_args


def _build_hls_output(stream_id: int, variant_key: str, ffmpeg_kwargs: dict, ll_hls: bool = False) -> Tuple[str, Dict[str, Any]]:
    playlist_path = _build_output_path(stream_id, variant_key, 'index.m3u8')
    _clean_output_path(playlist_path.parent)
    hls_flags = ffmpeg_kwargs.pop('hls_flags', '')
    if ll_hls:
        extra_flags = 'delete_segments+append_list+omit_endlist+program_date_time'
    else:
        extra_flags = 'delete_segments+omit_endlist'
    combined_flags = '+'.join(flag for flag in [hls_flags, extra_flags] if flag)

    output_kwargs = {
        'format': 'hls',
        'hls_time': ffmpeg_kwargs.pop('hls_time', 4),
        'hls_list_size': ffmpeg_kwargs.pop('hls_list_size', 0),
        'hls_flags': combined_flags,
        'strftime': ffmpeg_kwargs.pop('strftime', 0),
    }
    output_kwargs.update(ffmpeg_kwargs)
    return str(playlist_path), output_kwargs


def _build_dash_output(stream_id: int, variant_key: str, ffmpeg_kwargs: dict) -> Tuple[str, Dict[str, Any]]:
    manifest_path = _build_output_path(stream_id, variant_key, 'manifest.mpd')
    _clean_output_path(manifest_path.parent)
    output_kwargs = {
        'format': 'dash',
        'seg_duration': ffmpeg_kwargs.pop('seg_duration', 4),
        'remove_at_exit': ffmpeg_kwargs.pop('remove_at_exit', 1),
    }
    output_kwargs.update(ffmpeg_kwargs)
    return str(manifest_path), output_kwargs


def _build_audio_only_kwargs(ffmpeg_kwargs: dict) -> Dict[str, Any]:
    audio_kwargs = {'vn': None}
    audio_codec = ffmpeg_kwargs.pop('acodec', None) or 'aac'
    audio_kwargs['acodec'] = audio_codec
    audio_kwargs.update(ffmpeg_kwargs)
    return audio_kwargs


def _log_stderr(stream_name: str, process_key: Tuple[int, str], stderr_pipe):
    for line in iter(stderr_pipe.readline, b''):
        message = line.decode('utf-8', errors='replace').strip()
        if message:
            _stats_for(process_key)['last_stderr'] = message
        logging.getLogger('ffmpeg').info('[%s] %s', stream_name, message)


def get_or_start_stream_process(
    stream_config: dict,
    ffmpeg_profile: dict,
    process_variant: Optional[str] = None,
) -> Optional[subprocess.Popen]:
    stream_id = stream_config['id']
    stream_name = stream_config.get('name', f'Stream {stream_id}')
    variant_key = process_variant or DEFAULT_VARIANT_KEY
    process_key = (stream_id, variant_key)

    if process_key not in RUNNING_PROCESSES:
        RUNNING_PROCESSES[process_key] = {
            'process': None,
            'lock': threading.Lock(),
        }

    with RUNNING_PROCESSES[process_key]['lock']:
        proc_data = RUNNING_PROCESSES[process_key]
        process = proc_data.get('process')
        stats = _stats_for(process_key)

        if process and process.poll() is None:
            logging.info(
                "Returning existing FFmpeg process for stream '%s' (variant=%s, PID=%s)",
                stream_name,
                variant_key,
                process.pid,
            )
            return process

        if process and process.poll() is not None:
            stats['last_exit_code'] = process.returncode
            stats['last_stop_at'] = _now_iso()

        logging.info("Starting new FFmpeg process for stream '%s' (variant=%s)", stream_name, variant_key)
        try:
            custom_command = _prepare_custom_ffmpeg_command(stream_config)

            if custom_command:
                command, use_shell, env, cwd = custom_command
                logging.info("Launching custom FFmpeg command for '%s': %s", stream_name, command)
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=use_shell,
                    env=env,
                    cwd=cwd,
                )
                stats['last_source'] = 'custom_ffmpeg'
            else:
                resolved_source, handler_options = _resolve_stream_source(stream_config)
                if not resolved_source:
                    logging.error("Could not resolve an input source for stream '%s'.", stream_name)
                    stats['last_error'] = 'source_resolution_failed'
                    _emit_event('stream_failed', {'stream_id': stream_id, 'variant': variant_key, 'reason': 'source_resolution_failed'})
                    return None

                stats['last_source'] = resolved_source
                input_kwargs: Dict[str, Any] = {}
                input_kwargs.update(handler_options)

                configured_options = stream_config.get('input_options') or {}
                if configured_options and not isinstance(configured_options, dict):
                    logging.error("Stream '%s' has non-mapping input_options; ignoring the value.", stream_name)
                else:
                    input_kwargs.update(configured_options)

                input_args = stream_config.get('input_args') or []
                if input_args and not isinstance(input_args, list):
                    logging.error("Stream '%s' input_args must be a list of arguments; ignoring.", stream_name)
                    input_args = []

                input_stream = ffmpeg.input(resolved_source, *input_args, **input_kwargs)

                ffmpeg_options = dict(ffmpeg_profile)
                hwaccel_conf = ffmpeg_options.pop('hwaccel', None)
                input_stream, extra_global_args = _apply_hwaccel(input_stream, hwaccel_conf)

                audio_only = ffmpeg_options.pop('audio_only', False)
                ll_hls = ffmpeg_options.pop('ll_hls', False)
                output_format = ffmpeg_options.pop('output_format', 'ts')

                output_kwargs = dict(ffmpeg_options)
                output_target = 'pipe:1'

                if audio_only:
                    output_kwargs = _build_audio_only_kwargs(output_kwargs)

                if output_format in {'hls', 'll-hls'}:
                    output_target, output_kwargs = _build_hls_output(stream_id, variant_key, output_kwargs, ll_hls=(output_format == 'll-hls'))
                elif output_format == 'dash':
                    output_target, output_kwargs = _build_dash_output(stream_id, variant_key, output_kwargs)
                elif output_format == 'rtsp':
                    output_target = f'rtsp://127.0.0.1:8554/stream_{stream_id}_{variant_key}'
                elif output_format == 'audio':
                    output_target = 'pipe:1'
                    output_kwargs = _build_audio_only_kwargs(output_kwargs)
                elif output_format == 'mse':
                    output_target = 'pipe:1'
                    output_kwargs.setdefault('format', 'mp4')
                    movflags = output_kwargs.get('movflags')
                    default_flags = 'frag_keyframe+empty_moov+default_base_moof'
                    output_kwargs['movflags'] = f'{movflags}+{default_flags}' if movflags else default_flags
                    output_kwargs.setdefault('reset_timestamps', 1)
                elif output_format in {'websocket', 'ts'}:
                    output_target = 'pipe:1'
                    output_kwargs.setdefault('format', 'mpegts')

                output_stream = ffmpeg.output(input_stream, output_target, **output_kwargs)
                if extra_global_args:
                    output_stream = output_stream.global_args(*extra_global_args)

                process = output_stream.run_async(pipe_stdout=True, pipe_stderr=True)

            process.start_time = time.time()
            stats['starts'] += 1
            stats['restart_count'] = max(stats['starts'] - 1, 0)
            stats['last_start_at'] = _now_iso()
            stats['last_error'] = None
            proc_data['process'] = process
            logging.info("FFmpeg process started for '%s' (variant=%s) with PID: %s", stream_name, variant_key, process.pid)
            _emit_event('stream_started', {'stream_id': stream_id, 'name': stream_name, 'variant': variant_key, 'pid': process.pid})

            stderr_thread = threading.Thread(
                target=_log_stderr,
                args=(stream_name, process_key, process.stderr),
                daemon=True,
            )
            stderr_thread.start()
            return process

        except ffmpeg.Error as e:
            stats['last_error'] = e.stderr.decode('utf-8', errors='replace') if e.stderr else str(e)
            logging.error("FFmpeg error for stream '%s': %s", stream_name, stats['last_error'])
            _emit_event('stream_failed', {'stream_id': stream_id, 'name': stream_name, 'variant': variant_key, 'reason': stats['last_error']})
            return None
        except Exception as e:
            stats['last_error'] = str(e)
            logging.error("Failed to start FFmpeg for stream '%s': %s", stream_name, e)
            _emit_event('stream_failed', {'stream_id': stream_id, 'name': stream_name, 'variant': variant_key, 'reason': str(e)})
            return None


def stop_stream_process(stream_id: int, process_variant: Optional[str] = None):
    keys = [
        key for key in list(RUNNING_PROCESSES.keys())
        if key[0] == stream_id and (process_variant is None or key[1] == process_variant)
    ]

    for key in keys:
        with RUNNING_PROCESSES[key]['lock']:
            proc_data = RUNNING_PROCESSES.pop(key)
            process = proc_data.get('process')
            stats = _stats_for(key)
            if process and process.poll() is None:
                logging.warning("Terminating FFmpeg process for stream ID %s variant '%s' (PID: %s)", stream_id, key[1], process.pid)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logging.error('FFmpeg process %s did not terminate gracefully, killing.', process.pid)
                    process.kill()
                stats['last_exit_code'] = process.returncode
                stats['last_stop_at'] = _now_iso()
                logging.info("Process for stream ID %s variant '%s' stopped.", stream_id, key[1])
                _emit_event('stream_stopped', {'stream_id': stream_id, 'variant': key[1], 'exit_code': process.returncode})


def get_process_snapshot() -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    for key, stats in PROCESS_STATS.items():
        process = RUNNING_PROCESSES.get(key, {}).get('process')
        running = bool(process and process.poll() is None)
        snapshot = dict(stats)
        snapshot.update({
            'running': running,
            'pid': process.pid if process and running else None,
            'uptime_seconds': (time.time() - process.start_time) if process and running and hasattr(process, 'start_time') else None,
        })
        snapshots.append(snapshot)
    return snapshots


def cleanup_all_processes():
    logging.info('Shutting down all active FFmpeg streams...')
    stream_ids = {key[0] for key in RUNNING_PROCESSES.keys()}
    for stream_id in list(stream_ids):
        stop_stream_process(stream_id)
    logging.info('Cleanup complete.')


atexit.register(cleanup_all_processes)

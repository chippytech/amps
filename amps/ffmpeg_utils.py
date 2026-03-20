# amps/ffmpeg_utils.py

import ffmpeg
import logging
import shutil
import subprocess
import tempfile
import threading
import atexit
import shlex
from pathlib import Path
from typing import Dict, Optional, Tuple, Union, List, Any

try:
    import yt_dlp
except ImportError:  # pragma: no cover - dependency should be installed, but guard just in case
    yt_dlp = None

# Global dictionary to hold running FFmpeg processes and associated data
# Structure: { (stream_id, variant): {'process': Popen_object, 'lock': Lock_object} }
RUNNING_PROCESSES: Dict[Tuple[int, str], Dict] = {}
DEFAULT_VARIANT_KEY = 'default'
OUTPUT_BASE = Path(tempfile.gettempdir()) / 'amps_media'
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


def _resolve_stream_source(stream_config: dict) -> Tuple[Optional[str], Dict[str, Any]]:
    """Resolves the input source for FFmpeg, optionally using yt-dlp."""

    source = stream_config.get('source')
    if not source:
        logging.error("Stream '%s' is missing a source URL.", stream_config.get('name', stream_config.get('id')))
        return None, {}

    # Normalised configuration for yt-dlp usage. We support either the legacy
    # `use_yt_dlp` boolean or the richer `source_handler` mapping.
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

    # Allow users to pass additional yt-dlp options via `options` mapping.
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
    """Builds a custom FFmpeg command for a stream if configured."""

    custom_conf = stream_config.get('custom_ffmpeg')
    if not custom_conf:
        return None

    # Allow shorthand string for simple commands
    if isinstance(custom_conf, str):
        custom_conf = {'command': custom_conf}

    if not isinstance(custom_conf, dict):
        logging.error("custom_ffmpeg configuration must be a string or mapping.")
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
    """Constructs a deterministic output path for generated manifests and segments."""

    target_dir = OUTPUT_BASE / str(stream_id) / variant_key
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / filename


def _clean_output_path(path: Path):
    """Removes stale output for a variant before starting a new process."""

    if path.exists():
        try:
            if path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError:
            # Best-effort cleanup; continue even if deletion fails.
            logging.debug("Failed to clean previous output at %s", path)


def _apply_hwaccel(input_stream, hwaccel_conf: Optional[dict]):
    """Adds hardware acceleration arguments when requested."""

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
    """Prepares an HLS/LL-HLS output configuration."""

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
    """Prepares DASH output configuration."""

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

def _log_stderr(stream_name: str, stderr_pipe):
    """
    Reads from a process's stderr pipe and logs each line for debugging.
    """
    for line in iter(stderr_pipe.readline, b''):
        logging.getLogger('ffmpeg').info(f"[{stream_name}] {line.decode('utf-8').strip()}")

def get_or_start_stream_process(
    stream_config: dict,
    ffmpeg_profile: dict,
    process_variant: Optional[str] = None,
) -> Optional[subprocess.Popen]:
    """
    Retrieves a running FFmpeg process for a stream or starts a new one.
    This function is thread-safe.
    """
    stream_id = stream_config['id']
    stream_name = stream_config.get('name', f"Stream {stream_id}")

    # Initialize stream entry if not present
    variant_key = process_variant or DEFAULT_VARIANT_KEY
    process_key = (stream_id, variant_key)

    if process_key not in RUNNING_PROCESSES:
        RUNNING_PROCESSES[process_key] = {
            'process': None,
            'lock': threading.Lock()
        }

    with RUNNING_PROCESSES[process_key]['lock']:
        proc_data = RUNNING_PROCESSES[process_key]
        process = proc_data.get('process')

        # Check if process exists and is running
        if process and process.poll() is None:
            logging.info(
                "Returning existing FFmpeg process for stream '%s' (variant=%s, PID=%s)",
                stream_name,
                variant_key,
                process.pid,
            )
            return process

        # If process is dead or doesn't exist, start a new one
        logging.info("Starting new FFmpeg process for stream '%s' (variant=%s)", stream_name, variant_key)
        try:
            custom_command = _prepare_custom_ffmpeg_command(stream_config)

            if custom_command:
                command, use_shell, env, cwd = custom_command
                logging.info(f"Launching custom FFmpeg command for '{stream_name}': {command}")
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=use_shell,
                    env=env,
                    cwd=cwd,
                )
                logging.info(
                    "Custom FFmpeg process started for '%s' (variant=%s) with PID: %s",
                    stream_name,
                    variant_key,
                    process.pid,
                )
            else:
                resolved_source, handler_options = _resolve_stream_source(stream_config)
                if not resolved_source:
                    logging.error(
                        "Could not resolve an input source for stream '%s'.", stream_name
                    )
                    return None

                input_kwargs: Dict[str, Any] = {}
                input_kwargs.update(handler_options)

                configured_options = stream_config.get('input_options') or {}
                if configured_options and not isinstance(configured_options, dict):
                    logging.error(
                        "Stream '%s' has non-mapping input_options; ignoring the value.",
                        stream_name,
                    )
                else:
                    input_kwargs.update(configured_options)

                input_args = stream_config.get('input_args') or []
                if input_args and not isinstance(input_args, list):
                    logging.error(
                        "Stream '%s' input_args must be a list of arguments; ignoring.",
                        stream_name,
                    )
                    input_args = []

                logging.debug(
                    "FFmpeg input for '%s': source=%s, args=%s, options=%s",
                    stream_name,
                    resolved_source,
                    input_args,
                    input_kwargs,
                )

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
                    output_target, format_kwargs = _build_hls_output(stream_id, variant_key, output_kwargs, ll_hls=(output_format == 'll-hls'))
                    output_kwargs = format_kwargs
                elif output_format == 'dash':
                    output_target, format_kwargs = _build_dash_output(stream_id, variant_key, output_kwargs)
                    output_kwargs = format_kwargs
                elif output_format == 'rtsp':
                    # RTSP output requires a URL; we expose a unix socket style path for demo purposes.
                    output_target = f"rtsp://127.0.0.1:8554/stream_{stream_id}_{variant_key}"
                elif output_format == 'audio':
                    output_target = 'pipe:1'
                    output_kwargs = _build_audio_only_kwargs(output_kwargs)

                output_stream = ffmpeg.output(input_stream, output_target, **output_kwargs)
                if extra_global_args:
                    output_stream = output_stream.global_args(*extra_global_args)

                process = output_stream.run_async(pipe_stdout=True, pipe_stderr=True)
                logging.info(
                    "FFmpeg process started for '%s' (variant=%s) with PID: %s",
                    stream_name,
                    variant_key,
                    process.pid,
                )

            # Start a thread to log stderr for this process
            stderr_thread = threading.Thread(
                target=_log_stderr,
                args=(stream_name, process.stderr),
                daemon=True
            )
            stderr_thread.start()

            proc_data['process'] = process
            return process

        except ffmpeg.Error as e:
            logging.error(f"FFmpeg error for stream '{stream_name}': {e.stderr.decode('utf-8')}")
            return None
        except Exception as e:
            logging.error(f"Failed to start FFmpeg for stream '{stream_name}': {e}")
            return None

def stop_stream_process(stream_id: int, process_variant: Optional[str] = None):
    """
    Stops a specific FFmpeg process if it is running.
    """
    keys = [
        key for key in list(RUNNING_PROCESSES.keys())
        if key[0] == stream_id and (process_variant is None or key[1] == process_variant)
    ]

    for key in keys:
        with RUNNING_PROCESSES[key]['lock']:
            proc_data = RUNNING_PROCESSES.pop(key)
            process = proc_data.get('process')
            if process and process.poll() is None:
                logging.warning(
                    "Terminating FFmpeg process for stream ID %s variant '%s' (PID: %s)",
                    stream_id,
                    key[1],
                    process.pid,
                )
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logging.error(f"FFmpeg process {process.pid} did not terminate gracefully, killing.")
                    process.kill()
                logging.info(
                    "Process for stream ID %s variant '%s' stopped.",
                    stream_id,
                    key[1],
                )

def cleanup_all_processes():
    """
    Cleans up all running FFmpeg processes on application exit.
    """
    logging.info("Shutting down all active FFmpeg streams...")
    stream_ids = {key[0] for key in RUNNING_PROCESSES.keys()}
    for stream_id in list(stream_ids):
        stop_stream_process(stream_id)
    logging.info("Cleanup complete.")

# Register the cleanup function to be called on exit
atexit.register(cleanup_all_processes)

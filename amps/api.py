# amps/api.py

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from amps import ffmpeg_utils
from amps.auth_utils import ensure_request_auth
from amps.epg_importer import list_epg_status, refresh_epg_source
from amps.epg_utils import build_epg_payload
from amps.persistence import export_runtime_snapshot, persist_runtime_state, write_config_snapshot
from amps.recording_utils import export_clip, list_recordings, recording_status, start_recording, stop_recording
from amps.stream_utils import (
    extract_region_from_request,
    filter_streams,
    is_stream_allowed_for_region,
    parse_group_filter,
    parse_id_filter,
)
from amps.webhook_utils import dispatch_event

ALLOWED_STREAM_FIELDS = {
    'name',
    'source',
    'ffmpeg_profile',
    'logo',
    'tvg_name',
    'group',
    'channel_number',
    'next_programs',
    'custom_ffmpeg',
    'program_feed',
    'description',
    'input_options',
    'input_args',
    'source_handler',
    'use_yt_dlp',
    'yt_dlp_format',
    'epg_id',
    'regions_allowed',
    'regions_blocked',
    'adaptive_bitrates',
}

api_bp = Blueprint('api', __name__, url_prefix='/api')


def _viewer_counts():
    return current_app.config.setdefault('viewer_counts', {})


def _save_runtime_state():
    persist_runtime_state(current_app.config)


def _auth(min_role: str = 'viewer'):
    return ensure_request_auth(request, current_app.config, min_role=min_role)


def _validate_custom_ffmpeg(custom_ffmpeg):
    if custom_ffmpeg is None:
        return True, None
    if isinstance(custom_ffmpeg, str):
        return True, None
    if not isinstance(custom_ffmpeg, dict):
        return False, 'custom_ffmpeg must be a string command or mapping.'
    command = custom_ffmpeg.get('command')
    if not command:
        return False, "custom_ffmpeg requires a 'command' entry."
    if not isinstance(command, (str, list)):
        return False, "custom_ffmpeg 'command' must be a string or list of arguments."
    env = custom_ffmpeg.get('env')
    if env is not None and not isinstance(env, dict):
        return False, "custom_ffmpeg 'env' must be a mapping of environment variables."
    if 'shell' in custom_ffmpeg and not isinstance(custom_ffmpeg['shell'], bool):
        return False, "custom_ffmpeg 'shell' must be a boolean if provided."
    if 'cwd' in custom_ffmpeg and not isinstance(custom_ffmpeg['cwd'], str):
        return False, "custom_ffmpeg 'cwd' must be a string if provided."
    return True, None


def _validate_source_handler(handler):
    if handler is None:
        return True, None
    if not isinstance(handler, dict):
        return False, 'source_handler must be an object with handler configuration.'
    handler_type = (handler.get('type') or '').lower()
    if handler_type != 'yt_dlp':
        return False, f"Unsupported source_handler type '{handler.get('type')}'."
    fmt = handler.get('format')
    if fmt is not None and not isinstance(fmt, str):
        return False, 'source_handler.format must be a string when provided.'
    options = handler.get('options')
    if options is not None and not isinstance(options, dict):
        return False, 'source_handler.options must be an object mapping yt-dlp settings.'
    return True, None


def _validate_input_options(options):
    if options is None:
        return True, None
    if not isinstance(options, dict):
        return False, 'input_options must be an object mapping FFmpeg input keywords.'
    return True, None


def _validate_input_args(args):
    if args is None:
        return True, None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return False, 'input_args must be a list of strings.'
    return True, None


def _validate_next_programs(programs):
    if programs is None:
        return True, None
    if not isinstance(programs, list):
        return False, 'next_programs must be a list of program objects.'
    for idx, program in enumerate(programs):
        if not isinstance(program, dict):
            return False, f'Program entry at index {idx} must be an object.'
        if 'title' not in program:
            return False, f"Program entry at index {idx} missing required 'title'."
    return True, None


def _validate_region_list(field_name: str, regions):
    if regions is None:
        return True, None
    if not isinstance(regions, list) or not all(isinstance(region, str) for region in regions):
        return False, f'{field_name} must be a list of ISO country codes.'
    return True, None


def _validate_adaptive_bitrates(adaptive_bitrates, ffmpeg_profiles):
    if adaptive_bitrates is None:
        return True, None
    if not isinstance(adaptive_bitrates, list):
        return False, 'adaptive_bitrates must be a list of variant objects.'
    seen_names = set()
    for idx, variant in enumerate(adaptive_bitrates):
        if not isinstance(variant, dict):
            return False, f'adaptive_bitrates[{idx}] must be an object.'
        name = variant.get('name')
        if not name or not isinstance(name, str):
            return False, f"adaptive_bitrates[{idx}] requires a string 'name' field."
        if name in seen_names:
            return False, f"adaptive_bitrates contains duplicate variant name '{name}'."
        seen_names.add(name)
        profile = variant.get('ffmpeg_profile')
        if profile and profile not in ffmpeg_profiles:
            return False, f"adaptive_bitrates[{idx}] references unknown ffmpeg_profile '{profile}'."
    return True, None


def _stream_status(stream):
    stream_id = stream['id']
    snapshots = [item for item in ffmpeg_utils.get_process_snapshot() if item['stream_id'] == stream_id]
    running = any(item['running'] for item in snapshots)
    return {
        'stream_id': stream_id,
        'name': stream.get('name'),
        'running': running,
        'health': 'healthy' if running else ('error' if any(item.get('last_error') for item in snapshots) else 'idle'),
        'viewer_count': _viewer_counts().get(stream_id, 0),
        'recording': recording_status(stream_id),
        'variants': snapshots,
    }


@api_bp.before_request
def api_auth():
    role = 'admin' if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} else 'viewer'
    _auth(role)


@api_bp.route('/plugins', methods=['GET'])
def list_plugins():
    return jsonify({'loaded': current_app.config.get('loaded_plugins', []), 'failed': current_app.config.get('failed_plugins', [])})


@api_bp.route('/status', methods=['GET'])
def server_status():
    stream_map = current_app.config.get('stream_map', {})
    return jsonify({
        'streams': [_stream_status(stream) for stream in sorted(stream_map.values(), key=lambda item: item['id'])],
        'epg_sources': list_epg_status(current_app.config),
    })


@api_bp.route('/streams', methods=['GET'])
def get_streams():
    stream_map = current_app.config.get('stream_map', {})
    region = extract_region_from_request(request)
    groups = parse_group_filter(request.args.get('group'))
    ids = parse_id_filter(request.args.get('ids'))
    streams = list(filter_streams(stream_map.values(), region, groups, ids)) if (region or groups or ids) else list(stream_map.values())
    return jsonify(streams)


@api_bp.route('/streams/<int:stream_id>', methods=['GET'])
def get_stream(stream_id):
    stream = current_app.config.get('stream_map', {}).get(stream_id)
    if not stream:
        return jsonify({'error': 'Stream not found'}), 404
    region = extract_region_from_request(request)
    if region and not is_stream_allowed_for_region(stream, region):
        return jsonify({'error': 'Stream not available in this region'}), 403
    payload = dict(stream)
    payload['status'] = _stream_status(stream)
    return jsonify(payload)


@api_bp.route('/streams', methods=['POST'])
def add_stream():
    if not request.json or not all(k in request.json for k in ['name', 'source']):
        return jsonify({'error': 'Missing required fields: name, source'}), 400
    if 'ffmpeg_profile' not in request.json and 'custom_ffmpeg' not in request.json:
        return jsonify({'error': "Provide either 'ffmpeg_profile' or 'custom_ffmpeg' for a stream."}), 400

    stream_map = current_app.config.get('stream_map', {})
    new_id = max(stream_map.keys()) + 1 if stream_map else 1
    new_stream = {'id': new_id}
    for field in ALLOWED_STREAM_FIELDS:
        if field in request.json:
            new_stream[field] = request.json[field]

    validators = [
        _validate_custom_ffmpeg(new_stream.get('custom_ffmpeg')),
        _validate_source_handler(new_stream.get('source_handler')),
        _validate_input_options(new_stream.get('input_options')),
        _validate_input_args(new_stream.get('input_args')),
        _validate_next_programs(new_stream.get('next_programs')),
        _validate_region_list('regions_allowed', new_stream.get('regions_allowed')),
        _validate_region_list('regions_blocked', new_stream.get('regions_blocked')),
        _validate_adaptive_bitrates(new_stream.get('adaptive_bitrates'), current_app.config['ffmpeg_profiles']),
    ]
    for valid, error in validators:
        if not valid:
            return jsonify({'error': error}), 400
    if 'ffmpeg_profile' in new_stream and new_stream['ffmpeg_profile'] not in current_app.config['ffmpeg_profiles']:
        return jsonify({'error': f"ffmpeg_profile '{new_stream['ffmpeg_profile']}' not found"}), 400

    stream_map[new_id] = new_stream
    _save_runtime_state()
    dispatch_event(current_app.config.get('webhooks', []), 'stream_config_created', new_stream)
    return jsonify(new_stream), 201


@api_bp.route('/streams/<int:stream_id>', methods=['PUT'])
def update_stream(stream_id):
    stream_map = current_app.config.get('stream_map', {})
    if stream_id not in stream_map:
        return jsonify({'error': 'Stream not found'}), 404
    if not request.json:
        return jsonify({'error': 'Invalid JSON body'}), 400
    update_data = request.json

    validators = []
    if 'custom_ffmpeg' in update_data:
        validators.append(_validate_custom_ffmpeg(update_data.get('custom_ffmpeg')))
    if 'source_handler' in update_data:
        validators.append(_validate_source_handler(update_data.get('source_handler')))
    if 'input_options' in update_data:
        validators.append(_validate_input_options(update_data.get('input_options')))
    if 'input_args' in update_data:
        validators.append(_validate_input_args(update_data.get('input_args')))
    if 'next_programs' in update_data:
        validators.append(_validate_next_programs(update_data.get('next_programs')))
    if 'regions_allowed' in update_data:
        validators.append(_validate_region_list('regions_allowed', update_data.get('regions_allowed')))
    if 'regions_blocked' in update_data:
        validators.append(_validate_region_list('regions_blocked', update_data.get('regions_blocked')))
    if 'adaptive_bitrates' in update_data:
        validators.append(_validate_adaptive_bitrates(update_data.get('adaptive_bitrates'), current_app.config['ffmpeg_profiles']))
    for valid, error in validators:
        if not valid:
            return jsonify({'error': error}), 400

    if 'ffmpeg_profile' in update_data and update_data['ffmpeg_profile'] not in current_app.config['ffmpeg_profiles']:
        return jsonify({'error': f"ffmpeg_profile '{update_data['ffmpeg_profile']}' not found"}), 400

    if any(k in update_data for k in ['source', 'ffmpeg_profile', 'custom_ffmpeg']):
        ffmpeg_utils.stop_stream_process(stream_id)

    stream_entry = stream_map[stream_id]
    for key, value in update_data.items():
        if key == 'id':
            continue
        if value is None and key in stream_entry:
            stream_entry.pop(key)
        else:
            stream_entry[key] = value

    _save_runtime_state()
    dispatch_event(current_app.config.get('webhooks', []), 'stream_config_updated', {'stream_id': stream_id, 'changes': update_data})
    return jsonify(stream_entry)


@api_bp.route('/streams/<int:stream_id>', methods=['DELETE'])
def delete_stream(stream_id):
    stream_map = current_app.config.get('stream_map', {})
    if stream_id not in stream_map:
        return jsonify({'error': 'Stream not found'}), 404
    ffmpeg_utils.stop_stream_process(stream_id)
    deleted_stream = stream_map.pop(stream_id)
    _viewer_counts().pop(stream_id, None)
    _save_runtime_state()
    dispatch_event(current_app.config.get('webhooks', []), 'stream_config_deleted', {'stream_id': stream_id, 'name': deleted_stream.get('name')})
    return jsonify({'message': 'Stream deleted successfully', 'stream': deleted_stream})


@api_bp.route('/streams/<int:stream_id>/status', methods=['GET'])
def stream_status(stream_id):
    stream = current_app.config.get('stream_map', {}).get(stream_id)
    if not stream:
        return jsonify({'error': 'Stream not found'}), 404
    return jsonify(_stream_status(stream))


@api_bp.route('/streams/<int:stream_id>/restart', methods=['POST'])
def restart_stream(stream_id):
    stream = current_app.config.get('stream_map', {}).get(stream_id)
    if not stream:
        return jsonify({'error': 'Stream not found'}), 404
    ffmpeg_utils.stop_stream_process(stream_id)
    dispatch_event(current_app.config.get('webhooks', []), 'stream_restart_requested', {'stream_id': stream_id, 'name': stream.get('name')})
    return jsonify({'message': 'Restart requested', 'stream_id': stream_id})


@api_bp.route('/streams/<int:stream_id>/programs', methods=['GET', 'PUT'])
def manage_programs(stream_id):
    stream = current_app.config.get('stream_map', {}).get(stream_id)
    if not stream:
        return jsonify({'error': 'Stream not found'}), 404
    if request.method == 'GET':
        return jsonify(stream.get('next_programs', []))
    valid_programs, error = _validate_next_programs(request.json)
    if not valid_programs:
        return jsonify({'error': error}), 400
    stream['next_programs'] = request.json
    _save_runtime_state()
    return jsonify(stream['next_programs'])


@api_bp.route('/streams/<int:stream_id>/recordings', methods=['GET'])
def stream_recordings(stream_id):
    return jsonify({'active': recording_status(stream_id), 'items': list_recordings(current_app.config, stream_id)})


@api_bp.route('/streams/<int:stream_id>/recordings/start', methods=['POST'])
def start_stream_recording(stream_id):
    stream = current_app.config.get('stream_map', {}).get(stream_id)
    if not stream:
        return jsonify({'error': 'Stream not found'}), 404
    metadata = start_recording(stream, current_app.config)
    dispatch_event(current_app.config.get('webhooks', []), 'recording_started', metadata)
    return jsonify(metadata), 201


@api_bp.route('/streams/<int:stream_id>/recordings/stop', methods=['POST'])
def stop_stream_recording(stream_id):
    metadata = stop_recording(stream_id)
    if not metadata:
        return jsonify({'error': 'No active recording for this stream'}), 404
    dispatch_event(current_app.config.get('webhooks', []), 'recording_stopped', metadata)
    return jsonify(metadata)


@api_bp.route('/recordings', methods=['GET'])
def recordings_listing():
    return jsonify(list_recordings(current_app.config))


@api_bp.route('/recordings/<int:stream_id>/clip', methods=['POST'])
def recordings_clip(stream_id):
    payload = request.json or {}
    try:
        clip = export_clip(current_app.config, stream_id, payload['recording_file'], payload['start'], payload['end'])
    except KeyError:
        return jsonify({'error': 'recording_file, start, and end are required'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    dispatch_event(current_app.config.get('webhooks', []), 'recording_clip_exported', clip)
    return jsonify(clip), 201


@api_bp.route('/epg', methods=['GET'])
def epg_listing():
    stream_map = current_app.config.get('stream_map', {})
    region = extract_region_from_request(request)
    groups = parse_group_filter(request.args.get('group'))
    ids = parse_id_filter(request.args.get('ids'))
    filtered_streams = list(filter_streams(stream_map.values(), region, groups, ids))
    return jsonify(build_epg_payload(filtered_streams))


@api_bp.route('/epg/sources', methods=['GET'])
def epg_sources_listing():
    return jsonify(list_epg_status(current_app.config))


@api_bp.route('/epg/sources', methods=['POST'])
def add_epg_source():
    payload = request.json or {}
    if not payload.get('name'):
        return jsonify({'error': 'name is required'}), 400
    if not payload.get('url') and not payload.get('path'):
        return jsonify({'error': 'url or path is required'}), 400
    current_app.config.setdefault('epg_sources', []).append(payload)
    _save_runtime_state()
    return jsonify(payload), 201


@api_bp.route('/epg/sources/<source_name>/refresh', methods=['POST'])
def refresh_epg(source_name):
    source = next((entry for entry in current_app.config.get('epg_sources', []) if entry.get('name') == source_name), None)
    if not source:
        return jsonify({'error': 'EPG source not found'}), 404
    try:
        status = refresh_epg_source(current_app.config, source)
    except Exception as exc:
        dispatch_event(current_app.config.get('webhooks', []), 'epg_refresh_failed', {'name': source_name, 'error': str(exc)})
        return jsonify({'error': str(exc)}), 400
    _save_runtime_state()
    dispatch_event(current_app.config.get('webhooks', []), 'epg_refreshed', status)
    return jsonify(status)


@api_bp.route('/admin/export', methods=['GET'])
def export_config():
    _auth('admin')
    return jsonify(export_runtime_snapshot(current_app.config))


@api_bp.route('/admin/save', methods=['POST'])
def save_config_snapshot():
    _auth('admin')
    config_path = current_app.config.get('config_path', 'config.yaml')
    write_config_snapshot(current_app.config, config_path)
    _save_runtime_state()
    return jsonify({'message': 'Configuration snapshot saved', 'path': config_path})

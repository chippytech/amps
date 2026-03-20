# amps/api.py

from flask import Blueprint, jsonify, request, current_app
from amps import ffmpeg_utils
from amps.stream_utils import (
    extract_region_from_request,
    filter_streams,
    parse_group_filter,
    parse_id_filter,
    is_stream_allowed_for_region,
)
from amps.epg_utils import build_epg_payload


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


def _validate_custom_ffmpeg(custom_ffmpeg):
    if custom_ffmpeg is None:
        return True, None

    if isinstance(custom_ffmpeg, str):
        return True, None

    if not isinstance(custom_ffmpeg, dict):
        return False, "custom_ffmpeg must be a string command or mapping."

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
        return False, "source_handler must be an object with handler configuration."

    handler_type = (handler.get('type') or '').lower()

    if handler_type != 'yt_dlp':
        return False, f"Unsupported source_handler type '{handler.get('type')}'."

    fmt = handler.get('format')
    if fmt is not None and not isinstance(fmt, str):
        return False, "source_handler.format must be a string when provided."

    options = handler.get('options')
    if options is not None and not isinstance(options, dict):
        return False, "source_handler.options must be an object mapping yt-dlp settings."

    return True, None


def _validate_input_options(options):
    if options is None:
        return True, None

    if not isinstance(options, dict):
        return False, "input_options must be an object mapping FFmpeg input keywords."

    return True, None


def _validate_input_args(args):
    if args is None:
        return True, None

    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return False, "input_args must be a list of strings."

    return True, None


def _validate_next_programs(programs):
    if programs is None:
        return True, None

    if not isinstance(programs, list):
        return False, "next_programs must be a list of program objects."

    for idx, program in enumerate(programs):
        if not isinstance(program, dict):
            return False, f"Program entry at index {idx} must be an object."
        if 'title' not in program:
            return False, f"Program entry at index {idx} missing required 'title'."

    return True, None


def _validate_region_list(field_name: str, regions):
    if regions is None:
        return True, None

    if not isinstance(regions, list) or not all(isinstance(region, str) for region in regions):
        return False, f"{field_name} must be a list of ISO country codes."

    return True, None


def _validate_adaptive_bitrates(adaptive_bitrates, ffmpeg_profiles):
    if adaptive_bitrates is None:
        return True, None

    if not isinstance(adaptive_bitrates, list):
        return False, "adaptive_bitrates must be a list of variant objects."

    seen_names = set()
    for idx, variant in enumerate(adaptive_bitrates):
        if not isinstance(variant, dict):
            return False, f"adaptive_bitrates[{idx}] must be an object."

        name = variant.get('name')
        if not name or not isinstance(name, str):
            return False, f"adaptive_bitrates[{idx}] requires a string 'name' field."

        if name in seen_names:
            return False, f"adaptive_bitrates contains duplicate variant name '{name}'."
        seen_names.add(name)

        profile = variant.get('ffmpeg_profile')
        if profile and profile not in ffmpeg_profiles:
            return False, f"adaptive_bitrates[{idx}] references unknown ffmpeg_profile '{profile}'."

        if 'custom_ffmpeg' in variant:
            valid_custom, custom_error = _validate_custom_ffmpeg(variant.get('custom_ffmpeg'))
            if not valid_custom:
                return False, f"adaptive_bitrates[{idx}].custom_ffmpeg: {custom_error}"

        if 'input_options' in variant:
            valid_input, input_error = _validate_input_options(variant.get('input_options'))
            if not valid_input:
                return False, f"adaptive_bitrates[{idx}].input_options: {input_error}"

        if 'input_args' in variant:
            valid_args, args_error = _validate_input_args(variant.get('input_args'))
            if not valid_args:
                return False, f"adaptive_bitrates[{idx}].input_args: {args_error}"

    return True, None

api_bp = Blueprint('api', __name__, url_prefix='/api')

@api_bp.route('/streams', methods=['GET'])
def get_streams():
    """Returns the list of all configured streams."""
    stream_map = current_app.config.get('stream_map', {})
    region = extract_region_from_request(request)
    groups = parse_group_filter(request.args.get('group'))
    ids = parse_id_filter(request.args.get('ids'))

    if region or groups or ids:
        streams = list(filter_streams(stream_map.values(), region, groups, ids))
    else:
        streams = list(stream_map.values())

    return jsonify(streams)

@api_bp.route('/streams/<int:stream_id>', methods=['GET'])
def get_stream(stream_id):
    """Returns a single stream by its ID."""
    stream_map = current_app.config.get('stream_map', {})
    stream = stream_map.get(stream_id)
    if stream:
        region = extract_region_from_request(request)
        if region and not is_stream_allowed_for_region(stream, region):
            return jsonify({'error': 'Stream not available in this region'}), 403
        return jsonify(stream)
    return jsonify({'error': 'Stream not found'}), 404

@api_bp.route('/streams', methods=['POST'])
def add_stream():
    """Adds a new stream to the in-memory configuration."""
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

    if 'ffmpeg_profile' in new_stream:
        if new_stream['ffmpeg_profile'] not in current_app.config['ffmpeg_profiles']:
            return jsonify({'error': f"ffmpeg_profile '{new_stream['ffmpeg_profile']}' not found"}), 400

    valid_custom, custom_error = _validate_custom_ffmpeg(new_stream.get('custom_ffmpeg'))
    if not valid_custom:
        return jsonify({'error': custom_error}), 400

    if 'use_yt_dlp' in new_stream and not isinstance(new_stream['use_yt_dlp'], bool):
        return jsonify({'error': 'use_yt_dlp must be a boolean value.'}), 400

    if 'yt_dlp_format' in new_stream and new_stream['yt_dlp_format'] is not None and not isinstance(new_stream['yt_dlp_format'], str):
        return jsonify({'error': 'yt_dlp_format must be a string when provided.'}), 400

    valid_handler, handler_error = _validate_source_handler(new_stream.get('source_handler'))
    if not valid_handler:
        return jsonify({'error': handler_error}), 400

    valid_input_options, input_options_error = _validate_input_options(new_stream.get('input_options'))
    if not valid_input_options:
        return jsonify({'error': input_options_error}), 400

    valid_input_args, input_args_error = _validate_input_args(new_stream.get('input_args'))
    if not valid_input_args:
        return jsonify({'error': input_args_error}), 400

    valid_programs, programs_error = _validate_next_programs(new_stream.get('next_programs'))
    if not valid_programs:
        return jsonify({'error': programs_error}), 400

    valid_regions, regions_error = _validate_region_list('regions_allowed', new_stream.get('regions_allowed'))
    if not valid_regions:
        return jsonify({'error': regions_error}), 400

    valid_block_regions, block_error = _validate_region_list('regions_blocked', new_stream.get('regions_blocked'))
    if not valid_block_regions:
        return jsonify({'error': block_error}), 400

    valid_variants, variants_error = _validate_adaptive_bitrates(
        new_stream.get('adaptive_bitrates'),
        current_app.config['ffmpeg_profiles'],
    )
    if not valid_variants:
        return jsonify({'error': variants_error}), 400

    stream_map[new_id] = new_stream
    return jsonify(new_stream), 201

@api_bp.route('/streams/<int:stream_id>', methods=['PUT'])
def update_stream(stream_id):
    """Updates an existing stream."""
    stream_map = current_app.config.get('stream_map', {})
    if stream_id not in stream_map:
        return jsonify({'error': 'Stream not found'}), 404

    if not request.json:
        return jsonify({'error': 'Invalid JSON body'}), 400

    update_data = request.json
    if 'ffmpeg_profile' in update_data:
        if update_data['ffmpeg_profile'] not in current_app.config['ffmpeg_profiles']:
            return jsonify({'error': f"ffmpeg_profile '{update_data['ffmpeg_profile']}' not found"}), 400

    if 'custom_ffmpeg' in update_data:
        valid_custom, custom_error = _validate_custom_ffmpeg(update_data.get('custom_ffmpeg'))
        if not valid_custom:
            return jsonify({'error': custom_error}), 400

    if 'use_yt_dlp' in update_data and not isinstance(update_data['use_yt_dlp'], bool):
        return jsonify({'error': 'use_yt_dlp must be a boolean value.'}), 400

    if 'yt_dlp_format' in update_data and update_data['yt_dlp_format'] is not None and not isinstance(update_data['yt_dlp_format'], str):
        return jsonify({'error': 'yt_dlp_format must be a string when provided.'}), 400

    if 'source_handler' in update_data:
        valid_handler, handler_error = _validate_source_handler(update_data.get('source_handler'))
        if not valid_handler:
            return jsonify({'error': handler_error}), 400

    if 'input_options' in update_data:
        valid_input_options, input_options_error = _validate_input_options(update_data.get('input_options'))
        if not valid_input_options:
            return jsonify({'error': input_options_error}), 400

    if 'input_args' in update_data:
        valid_input_args, input_args_error = _validate_input_args(update_data.get('input_args'))
        if not valid_input_args:
            return jsonify({'error': input_args_error}), 400

    if 'next_programs' in update_data:
        valid_programs, programs_error = _validate_next_programs(update_data.get('next_programs'))
        if not valid_programs:
            return jsonify({'error': programs_error}), 400

    if 'regions_allowed' in update_data:
        valid_regions, regions_error = _validate_region_list('regions_allowed', update_data.get('regions_allowed'))
        if not valid_regions:
            return jsonify({'error': regions_error}), 400

    if 'regions_blocked' in update_data:
        valid_block_regions, block_error = _validate_region_list('regions_blocked', update_data.get('regions_blocked'))
        if not valid_block_regions:
            return jsonify({'error': block_error}), 400

    if 'adaptive_bitrates' in update_data:
        valid_variants, variants_error = _validate_adaptive_bitrates(
            update_data.get('adaptive_bitrates'),
            current_app.config['ffmpeg_profiles'],
        )
        if not valid_variants:
            return jsonify({'error': variants_error}), 400

    # Stop the old process if source or profile changes
    if any(k in update_data for k in ['source', 'ffmpeg_profile', 'custom_ffmpeg']):
        ffmpeg_utils.stop_stream_process(stream_id)

    stream_entry = stream_map[stream_id]

    # Update while respecting optional removals (None removes field)
    for key, value in update_data.items():
        if key == 'id':
            continue
        if key not in ALLOWED_STREAM_FIELDS | {'id'}:
            stream_entry[key] = value
            continue

        if value is None and key in stream_entry:
            stream_entry.pop(key)
        else:
            stream_entry[key] = value

    return jsonify(stream_map[stream_id])

@api_bp.route('/streams/<int:stream_id>', methods=['DELETE'])
def delete_stream(stream_id):
    """Deletes a stream and stops its FFmpeg process."""
    stream_map = current_app.config.get('stream_map', {})
    if stream_id in stream_map:
        ffmpeg_utils.stop_stream_process(stream_id)
        deleted_stream = stream_map.pop(stream_id)
        return jsonify({'message': 'Stream deleted successfully', 'stream': deleted_stream})
    return jsonify({'error': 'Stream not found'}), 404


@api_bp.route('/streams/<int:stream_id>/programs', methods=['GET', 'PUT'])
def manage_programs(stream_id):
    """Retrieves or replaces the upcoming program schedule for a stream."""

    stream_map = current_app.config.get('stream_map', {})
    stream = stream_map.get(stream_id)

    if not stream:
        return jsonify({'error': 'Stream not found'}), 404

    if request.method == 'GET':
        return jsonify(stream.get('next_programs', []))

    if not request.json:
        return jsonify({'error': 'Invalid JSON body'}), 400

    valid_programs, error = _validate_next_programs(request.json)
    if not valid_programs:
        return jsonify({'error': error}), 400

    stream['next_programs'] = request.json
    return jsonify(stream['next_programs'])


@api_bp.route('/epg', methods=['GET'])
def epg_listing():
    """Returns the EPG payload for accessible streams."""

    stream_map = current_app.config.get('stream_map', {})
    region = extract_region_from_request(request)
    groups = parse_group_filter(request.args.get('group'))
    ids = parse_id_filter(request.args.get('ids'))

    filtered_streams = list(filter_streams(stream_map.values(), region, groups, ids))
    return jsonify(build_epg_payload(filtered_streams))

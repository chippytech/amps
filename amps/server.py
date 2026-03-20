# amps/server.py

import time
import logging
import atexit
import copy
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, request, abort, url_for, jsonify, send_from_directory

from amps import ffmpeg_utils
from amps.api import api_bp
from amps.stream_utils import (
    extract_region_from_request,
    filter_streams,
    parse_group_filter,
    parse_id_filter,
    is_stream_allowed_for_region,
)
from amps.epg_utils import build_xmltv

START_TIME = time.time()


def _parse_schedule_datetime(value, stream_label: str, field_name: str) -> Optional[datetime]:
    """Parses ISO-8601 datetime strings into aware UTC datetime objects."""

    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith('Z'):
            candidate = candidate[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            logging.error(
                "Scheduled stream '%s' has invalid %s '%s'. Expected ISO-8601 format.",
                stream_label,
                field_name,
                value,
            )
            return None
    else:
        logging.error(
            "Scheduled stream '%s' has unsupported %s type '%s'.",
            stream_label,
            field_name,
            type(value).__name__,
        )
        return None

    if dt.tzinfo is None:
        logging.warning(
            "Scheduled stream '%s' %s '%s' is naive. Assuming UTC.",
            stream_label,
            field_name,
            value,
        )
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def create_app(config: dict) -> Flask:
    """
    Creates and configures the Flask application.
    """
    app = Flask(__name__)
    app.config.update(config)
    app.config.setdefault('stream_map', {})
    app.config.setdefault('scheduled_streams', [])
    app.config.setdefault('media_root', str(ffmpeg_utils.OUTPUT_BASE))

    # Register API blueprint
    app.register_blueprint(api_bp)

    scheduler = BackgroundScheduler()
    scheduler.start()
    app.extensions['apscheduler'] = scheduler

    def shutdown_scheduler():
        if scheduler.running:
            scheduler.shutdown(wait=False)

    atexit.register(shutdown_scheduler)

    def activate_scheduled_stream(stream_id: int, stream_definition: dict):
        stream_map = app.config.setdefault('stream_map', {})
        if stream_id in stream_map:
            logging.info(
                "Scheduled stream '%s' (ID %s) is already active.",
                stream_definition.get('name', stream_id),
                stream_id,
            )
            return

        stream_map[stream_id] = copy.deepcopy(stream_definition)
        logging.info(
            "Activated scheduled stream '%s' (ID %s).",
            stream_definition.get('name', stream_id),
            stream_id,
        )

    def deactivate_scheduled_stream(stream_id: int):
        stream_map = app.config.setdefault('stream_map', {})
        stream_definition = stream_map.get(stream_id)
        if not stream_definition:
            return

        ffmpeg_utils.stop_stream_process(stream_id)
        stream_map.pop(stream_id, None)
        logging.info(
            "Deactivated scheduled stream '%s' (ID %s).",
            stream_definition.get('name', stream_id),
            stream_id,
        )

    def setup_scheduled_streams():
        scheduled_streams = app.config.get('scheduled_streams', [])
        if not scheduled_streams:
            return

        now = datetime.now(timezone.utc)
        base_stream_ids = {
            stream['id']
            for stream in app.config.get('streams', [])
            if isinstance(stream, dict) and 'id' in stream
        }

        for entry in scheduled_streams:
            if not isinstance(entry, dict):
                logging.error("Skipping malformed scheduled stream definition: %s", entry)
                continue

            stream_id = entry.get('id')
            if stream_id is None:
                logging.error("Scheduled stream missing required 'id': %s", entry)
                continue

            if stream_id in base_stream_ids:
                logging.warning(
                    "Skipping scheduled stream '%s' (ID %s) because a static stream with the same ID exists.",
                    entry.get('name', stream_id),
                    stream_id,
                )
                continue

            schedule_conf = entry.get('schedule', {}) or {}
            stream_label = entry.get('name', stream_id)
            start_dt = _parse_schedule_datetime(schedule_conf.get('start'), stream_label, 'start time')
            end_dt = _parse_schedule_datetime(schedule_conf.get('end'), stream_label, 'end time')

            if start_dt and end_dt and end_dt <= start_dt:
                logging.warning(
                    "Scheduled stream '%s' (ID %s) has an end time before the start time. Skipping.",
                    stream_label,
                    stream_id,
                )
                continue

            stream_definition = copy.deepcopy(entry)

            if end_dt and end_dt <= now:
                deactivate_scheduled_stream(stream_id)
                continue

            if not start_dt or start_dt <= now:
                activate_scheduled_stream(stream_id, stream_definition)
            else:
                scheduler.add_job(
                    activate_scheduled_stream,
                    trigger='date',
                    run_date=start_dt,
                    args=[stream_id, stream_definition],
                    id=f'stream_{stream_id}_activate',
                    replace_existing=True,
                )
                logging.info(
                    "Scheduled activation for stream '%s' (ID %s) at %s.",
                    stream_label,
                    stream_id,
                    start_dt.isoformat(),
                )

            if end_dt:
                scheduler.add_job(
                    deactivate_scheduled_stream,
                    trigger='date',
                    run_date=end_dt,
                    args=[stream_id],
                    id=f'stream_{stream_id}_deactivate',
                    replace_existing=True,
                )
                logging.info(
                    "Scheduled deactivation for stream '%s' (ID %s) at %s.",
                    stream_label,
                    stream_id,
                    end_dt.isoformat(),
                )

    setup_scheduled_streams()

    def build_stream_url(stream_id: int, extra_params: Optional[dict] = None) -> str:
        """Constructs an absolute streaming URL with auth and optional query args."""

        query_params = {}
        if app.config['auth']['enabled']:
            query_params['token'] = app.config['auth']['token']

        if extra_params:
            for key, value in extra_params.items():
                if value is None:
                    continue
                query_params[key] = value

        stream_url = url_for('stream_media', stream_id=stream_id, _external=True)
        if query_params:
            return f"{stream_url}?{urlencode(query_params)}"
        return stream_url

    def _static_manifest_response(stream_id: int, variant_key: str, filename: str):
        base = ffmpeg_utils.OUTPUT_BASE / str(stream_id) / variant_key
        if not base.exists():
            abort(404, description=f"No generated output for stream {stream_id} variant {variant_key}.")
        return send_from_directory(base, filename)

    @app.before_request
    def auth_middleware():
        """Enforces token authentication on protected routes."""
        if request.path == '/metrics' or not app.config['auth']['enabled']:
            return  # Skip auth for metrics or if auth is disabled

        token = request.headers.get('X-Amps-Token') or request.args.get('token')
        if not token or token != app.config['auth']['token']:
            logging.warning(f"Unauthorized access attempt from {request.remote_addr}")
            abort(401, description="Unauthorized: Valid token required.")

    @app.route('/playlist.m3u')
    def generate_playlist():
        """Generates a dynamic, filterable M3U playlist."""

        m3u_content = ['#EXTM3U']
        stream_map = app.config.get('stream_map', {})
        region = extract_region_from_request(request)
        groups = parse_group_filter(request.args.get('group'))
        ids = parse_id_filter(request.args.get('ids'))
        include_variants = request.args.get('variants', 'true').lower() not in {'false', '0', 'no'}

        base_query = {}
        if region:
            base_query['region'] = region

        filtered_streams = sorted(filter_streams(stream_map.values(), region, groups, ids), key=lambda s: s['id'])

        for stream in filtered_streams:
            tvg_name = stream.get('tvg_name') or stream.get('name')
            channel_identifier = stream.get('epg_id') or stream.get('tvg_id') or stream.get('id')
            attributes = [f'tvg-id="{channel_identifier}"']
            if tvg_name:
                attributes.append(f'tvg-name="{tvg_name}"')
            if stream.get('logo'):
                attributes.append(f'tvg-logo="{stream["logo"]}"')
            if stream.get('group'):
                attributes.append(f'group-title="{stream["group"]}"')
            if stream.get('channel_number'):
                attributes.append(f'channel-number="{stream["channel_number"]}"')

            entries = [
                {
                    'name': stream.get('name'),
                    'params': {},
                    'variant': None,
                }
            ]

            if include_variants:
                for variant in stream.get('adaptive_bitrates') or []:
                    variant_name = variant.get('name')
                    if not variant_name:
                        continue
                    label = variant.get('label') or variant_name.upper()
                    entries.append({
                        'name': f"{stream.get('name')} ({label})",
                        'params': {'variant': variant_name},
                        'variant': variant_name,
                    })

            for entry in entries:
                query = dict(base_query)
                query.update(entry['params'])
                stream_url = build_stream_url(stream['id'], query)
                m3u_content.append(f'#EXTINF:-1 {" ".join(attributes)},{entry["name"]}')

                if entry['variant']:
                    m3u_content.append(f'#EXTREM:AMP-VARIANT name="{entry["variant"]}"')

                next_programs = stream.get('next_programs') or []
                if next_programs:
                    next_program = next_programs[0]
                    program_parts = []
                    if next_program.get('title'):
                        program_parts.append(f'title="{next_program["title"]}"')
                    if next_program.get('start'):
                        program_parts.append(f'start="{next_program["start"]}"')
                    if next_program.get('description'):
                        program_parts.append(f'description="{next_program["description"]}"')
                    if program_parts:
                        m3u_content.append(f'#EXTREM:AMP-NEXT {" ".join(program_parts)}')

                if stream.get('program_feed'):
                    m3u_content.append(f'#EXTREM:AMP-PROGRAM-FEED url="{stream["program_feed"]}"')

                if stream.get('description'):
                    m3u_content.append(f'#EXTREM:AMP-DESCRIPTION {stream["description"]}')

                if stream.get('regions_allowed') or stream.get('regions_blocked'):
                    allowed = ','.join(stream.get('regions_allowed') or [])
                    blocked = ','.join(stream.get('regions_blocked') or [])
                    region_parts = []
                    if allowed:
                        region_parts.append(f'allow={allowed}')
                    if blocked:
                        region_parts.append(f'block={blocked}')
                    if region_parts:
                        m3u_content.append(f'#EXTREM:AMP-REGION {" ".join(region_parts)}')

                m3u_content.append(stream_url)

        return Response('\n'.join(m3u_content), mimetype='application/vnd.apple.mpegurl')

    @app.route('/stream/<int:stream_id>')
    def stream_media(stream_id):
        """Looks up a stream, runs FFmpeg, and pipes the output."""
        stream_map = app.config.get('stream_map', {})
        stream_config = stream_map.get(stream_id)

        if not stream_config:
            abort(404, description=f"Stream with ID {stream_id} not found.")

        region = extract_region_from_request(request)
        if not is_stream_allowed_for_region(stream_config, region):
            abort(403, description=f"Stream {stream_id} is not available in your region.")

        variant_name = request.args.get('variant')
        variant_config = None
        selected_stream_config = stream_config

        if variant_name:
            for candidate in stream_config.get('adaptive_bitrates') or []:
                if candidate.get('name') == variant_name:
                    variant_config = candidate
                    break

            if not variant_config:
                abort(404, description=f"Variant '{variant_name}' not found for stream {stream_id}.")

            selected_stream_config = copy.deepcopy(stream_config)
            for field in ['ffmpeg_profile', 'custom_ffmpeg', 'source', 'input_options', 'input_args', 'source_handler', 'use_yt_dlp', 'yt_dlp_format']:
                if field in variant_config:
                    selected_stream_config[field] = variant_config[field]

        custom_ffmpeg = selected_stream_config.get('custom_ffmpeg')
        ffmpeg_profile_name = selected_stream_config.get('ffmpeg_profile')

        if custom_ffmpeg:
            ffmpeg_profile = app.config['ffmpeg_profiles'].get(ffmpeg_profile_name, {}) if ffmpeg_profile_name else {}
        else:
            if not ffmpeg_profile_name:
                abort(500, description=f"Stream {stream_id} is missing an ffmpeg_profile configuration.")
            ffmpeg_profile = app.config['ffmpeg_profiles'].get(ffmpeg_profile_name)
            if not ffmpeg_profile:
                abort(500, description=f"FFmpeg profile '{ffmpeg_profile_name}' not found for stream {stream_id}.")

        process_variant = variant_name if variant_config else None
        process = ffmpeg_utils.get_or_start_stream_process(selected_stream_config, ffmpeg_profile, process_variant=process_variant)

        if not process:
            abort(500, description=f"Failed to start FFmpeg for stream {stream_id}.")

        def generate_chunks():
            try:
                # Read in chunks to stream the data
                while True:
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        logging.warning(f"Stream {stream_id} ended or FFmpeg process died.")
                        break
                    yield chunk
            except Exception as e:
                logging.error(f"Error while streaming for stream {stream_id}: {e}")
            finally:
                logging.info(f"Client disconnected from stream {stream_id}.")
                # Note: We don't kill the process here, as other clients might be connected.
                # The process will be restarted on next request if it dies.

        # MPEG-TS is the transport stream format specified in ffmpeg_profiles
        return Response(generate_chunks(), mimetype='video/mp2t')

    @app.route('/hls/<int:stream_id>/<path:filename>')
    def hls_manifest(stream_id, filename):
        return _static_manifest_response(stream_id, 'hls', filename)

    @app.route('/dash/<int:stream_id>/<path:filename>')
    def dash_manifest(stream_id, filename):
        return _static_manifest_response(stream_id, 'dash', filename)

    @app.route('/audio/<int:stream_id>')
    def audio_only(stream_id):
        stream_map = app.config.get('stream_map', {})
        stream_config = stream_map.get(stream_id)
        if not stream_config:
            abort(404, description=f"Stream with ID {stream_id} not found.")

        audio_config = copy.deepcopy(stream_config)
        audio_config['ffmpeg_profile'] = stream_config.get('ffmpeg_profile')
        audio_config['output_format'] = 'audio'

        ffmpeg_profiles = app.config.get('ffmpeg_profiles', {})
        profile = ffmpeg_profiles.get(audio_config['ffmpeg_profile'], {})
        audio_profile = dict(profile)
        audio_profile.setdefault('audio_only', True)
        audio_profile.setdefault('output_format', 'audio')

        process = ffmpeg_utils.get_or_start_stream_process(audio_config, audio_profile, process_variant='audio')
        if not process:
            abort(500, description=f"Failed to start FFmpeg for stream {stream_id} (audio).")

        def generate_audio():
            try:
                for chunk in iter(lambda: process.stdout.read(4096), b''):
                    if not chunk:
                        break
                    yield chunk
            except Exception as exc:  # pragma: no cover - stream interruptions
                logging.error("Audio streaming error for %s: %s", stream_id, exc)

        return Response(generate_audio(), mimetype='audio/aac')

    @app.route('/epg.xml')
    def xmltv():
        """Outputs an XMLTV feed generated from configured schedules."""

        stream_map = app.config.get('stream_map', {})
        region = extract_region_from_request(request)
        groups = parse_group_filter(request.args.get('group'))
        ids = parse_id_filter(request.args.get('ids'))

        base_query = {}
        if region:
            base_query['region'] = region

        annotated_streams = []
        for stream in filter_streams(stream_map.values(), region, groups, ids):
            stream_copy = copy.deepcopy(stream)
            stream_copy['_stream_url'] = build_stream_url(stream['id'], base_query)
            annotated_streams.append(stream_copy)

        xml_payload = build_xmltv(annotated_streams)
        return Response(xml_payload, mimetype='application/xml')

    @app.route('/metrics')
    def metrics():
        """Returns simple server metrics."""
        uptime_seconds = time.time() - START_TIME
        return jsonify({
            'uptime_seconds': uptime_seconds,
            'stream_count': len(app.config.get('stream_map', {})),
            'active_ffmpeg_processes': sum(1 for p_data in ffmpeg_utils.RUNNING_PROCESSES.values() if p_data['process'] and p_data['process'].poll() is None)
        })

    return app

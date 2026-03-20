# amps/cli.py

import click
import logging
import sys
from amps.config_loader import load_config
from amps.server import create_app

@click.group()
def main_cli():
    """
    Amps - Advanced Media Playlist Server
    A dynamic M3U playlist and media streaming server powered by FFmpeg.
    """
    # Configure logging
    log_format = '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s'
    logging.basicConfig(level=logging.INFO, format=log_format, stream=sys.stdout)
    # Silence overly verbose libraries
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    # Create a separate logger for ffmpeg output
    logging.getLogger('ffmpeg')


@main_cli.command('serve')
@click.option('--config', default='config.yaml', help='Path to the YAML configuration file.', type=click.Path(exists=True))
def serve_command(config):
    """Starts the Amps Flask server."""
    app_config = load_config(config)
    app = create_app(app_config)

    server_conf = app_config['server']
    host = server_conf['host']
    port = server_conf['port']
    debug = server_conf['debug']

    print("---" * 10)
    print("⚡ Amps – Advanced Media Playlist Server")
    if app_config['auth']['enabled']:
        print("🔒 Authentication: Enabled")
    else:
        print("🔓 Authentication: Disabled")
    print(f"📡 Serving {len(app_config['stream_map'])} streams at http://{host}:{port}")
    scheduled_count = len(app_config.get('scheduled_streams', []))
    if scheduled_count:
        print(f"🕒 {scheduled_count} scheduled stream(s) configured")
    if debug:
        print("⚠️  Running in DEBUG mode. Do not use in production.")
    print("---" * 10)

    if debug:
        # Use Flask's built-in development server
        app.run(host=host, port=port, debug=True)
    else:
        # Use a production-ready WSGI server like Gunicorn
        # Note: For this to work, 'gunicorn' must be installed.
        try:
            from gunicorn.app.base import BaseApplication

            class StandaloneApplication(BaseApplication):
                def __init__(self, app, options=None):
                    self.options = options or {}
                    self.application = app
                    super().__init__()

                def load_config(self):
                    for key, value in self.options.items():
                        self.cfg.set(key.lower(), value)

                def load(self):
                    return self.application

            options = {
                'bind': f'{host}:{port}',
                'workers': 4, # A sensible default
                'threads': 4,
                'worker_class': 'gthread',
                'timeout': 120
            }
            StandaloneApplication(app, options).run()
        except ImportError:
            logging.error("Gunicorn not found. Please install it (`pip install gunicorn`) to run in production mode.")
            logging.warning("Falling back to Flask's development server. DO NOT USE IN PRODUCTION.")
            app.run(host=host, port=port)


@main_cli.command('list')
@click.option('--config', default='config.yaml', help='Path to the YAML configuration file.', type=click.Path(exists=True))
def list_command(config):
    """Lists all available streams from the configuration."""
    app_config = load_config(config)
    streams = app_config.get('streams', [])
    if not streams:
        click.echo("No streams found in the configuration.")
        return

    click.echo("Available Streams:")
    for stream in streams:
        profile = stream.get('ffmpeg_profile')
        custom = stream.get('custom_ffmpeg')

        if custom and profile:
            profile_label = f"{profile} (custom override)"
        elif custom and not profile:
            profile_label = "custom command"
        else:
            profile_label = profile or "—"

        logo = stream.get('logo') or "—"
        click.echo(f"  - ID: {stream['id']}, Name: {stream['name']}, Profile: {profile_label}, Logo: {logo}")

if __name__ == '__main__':
    main_cli()

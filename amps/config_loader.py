# amps/config_loader.py

import yaml
import logging
import copy

DEFAULT_CONFIG = {
    'server': {
        'host': '0.0.0.0',
        'port': 5000,
        'debug': False,
    },
    'auth': {
        'enabled': True,
        'token': 'changeme123',
    },
    'streams': [],
    'scheduled_streams': [],
    'ffmpeg_profiles': {},
    'plugins': [],
}

def load_config(config_path: str) -> dict:
    """
    Loads, validates, and provides defaults for the YAML configuration.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(config_path, 'r') as f:
            user_config = yaml.safe_load(f)
            if not user_config:
                logging.warning(f"Configuration file '{config_path}' is empty. Using defaults.")
                return config

            # Deep merge user config into default config
            if 'server' in user_config:
                config['server'].update(user_config['server'])
            if 'auth' in user_config:
                config['auth'].update(user_config['auth'])
            if 'streams' in user_config:
                config['streams'] = user_config['streams']
            if 'scheduled_streams' in user_config:
                config['scheduled_streams'] = user_config['scheduled_streams']
            if 'ffmpeg_profiles' in user_config:
                config['ffmpeg_profiles'] = user_config['ffmpeg_profiles']
            if 'plugins' in user_config:
                config['plugins'] = user_config['plugins']

        logging.info(f"Loaded configuration from '{config_path}'.")
        logging.info(
            f"Loaded {len(config['streams'])} streams, "
            f"{len(config['scheduled_streams'])} scheduled streams and "
            f"{len(config['ffmpeg_profiles'])} FFmpeg profiles."
        )

        # In-memory store for API modifications
        config['stream_map'] = {stream['id']: stream for stream in config['streams']}

        scheduled_ids = {
            stream['id'] for stream in config['scheduled_streams']
            if isinstance(stream, dict) and 'id' in stream
        }

        duplicate_ids = scheduled_ids.intersection(config['stream_map'].keys())
        if duplicate_ids:
            logging.warning(
                "Scheduled streams share IDs with static streams: %s. "
                "Static stream definitions take precedence.",
                ', '.join(str(stream_id) for stream_id in sorted(duplicate_ids))
            )

        return config

    except FileNotFoundError:
        logging.error(f"Configuration file not found at '{config_path}'. Aborting.")
        exit(1)
    except yaml.YAMLError as e:
        logging.error(f"Error parsing YAML file '{config_path}': {e}. Aborting.")
        exit(1)
    except Exception as e:
        logging.error(f"An unexpected error occurred while loading config: {e}. Aborting.")
        exit(1)

# amps/config_loader.py

import copy
import logging

import yaml

from amps.persistence import load_persisted_state

DEFAULT_CONFIG = {
    'server': {
        'host': '0.0.0.0',
        'port': 5000,
        'debug': False,
    },
    'auth': {
        'enabled': True,
        'token': 'changeme123',
        'api_keys': [],
    },
    'streams': [],
    'scheduled_streams': [],
    'ffmpeg_profiles': {},
    'plugins': [],
    'webhooks': [],
    'epg_sources': [],
    'data_root': './data',
}


def load_config(config_path: str) -> dict:
    """
    Loads, validates, and provides defaults for the YAML configuration.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            user_config = yaml.safe_load(f)
            if not user_config:
                logging.warning("Configuration file '%s' is empty. Using defaults.", config_path)
                config['config_path'] = config_path
                return config

            # Deep merge user config into default config
            if 'server' in user_config:
                config['server'].update(user_config['server'])
            if 'auth' in user_config:
                config['auth'].update(user_config['auth'])
            for key in ['streams', 'scheduled_streams', 'ffmpeg_profiles', 'plugins', 'webhooks', 'epg_sources']:
                if key in user_config:
                    config[key] = user_config[key]
            if 'data_root' in user_config:
                config['data_root'] = user_config['data_root']

        config['config_path'] = config_path
        config = load_persisted_state(config)

        logging.info("Loaded configuration from '%s'.", config_path)
        logging.info(
            "Loaded %s streams, %s scheduled streams and %s FFmpeg profiles.",
            len(config['streams']),
            len(config['scheduled_streams']),
            len(config['ffmpeg_profiles']),
        )

        # In-memory store for API modifications
        config['stream_map'] = {stream['id']: stream for stream in config['streams'] if isinstance(stream, dict) and 'id' in stream}

        scheduled_ids = {
            stream['id'] for stream in config['scheduled_streams']
            if isinstance(stream, dict) and 'id' in stream
        }

        duplicate_ids = scheduled_ids.intersection(config['stream_map'].keys())
        if duplicate_ids:
            logging.warning(
                "Scheduled streams share IDs with static streams: %s. Static stream definitions take precedence.",
                ', '.join(str(stream_id) for stream_id in sorted(duplicate_ids))
            )

        return config

    except FileNotFoundError:
        logging.error("Configuration file not found at '%s'. Aborting.", config_path)
        raise SystemExit(1)
    except yaml.YAMLError as e:
        logging.error("Error parsing YAML file '%s': %s. Aborting.", config_path, e)
        raise SystemExit(1)
    except Exception as e:
        logging.error("An unexpected error occurred while loading config: %s. Aborting.", e)
        raise SystemExit(1)

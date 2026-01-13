"""Helper utilities for loading and initializing Amps plugins."""

import importlib
import inspect
import logging
from typing import Any, Dict, Iterable, List, Tuple


PluginConfig = Dict[str, Any]


def _extract_plugin_fields(plugin_entry: Any) -> Tuple[str, PluginConfig]:
    """Normalizes a plugin entry into a module path and configuration mapping."""

    if isinstance(plugin_entry, str):
        return plugin_entry, {}

    if isinstance(plugin_entry, dict):
        module = plugin_entry.get('module')
        config = plugin_entry.get('config') or {}
        return module, config if isinstance(config, dict) else {}

    logging.warning("Skipping plugin entry with unsupported type %s", type(plugin_entry).__name__)
    return '', {}


def load_plugins(app, plugin_entries: Iterable[Any], api_blueprint=None) -> List[str]:
    """Import and initialize plugins declared in configuration.

    Each plugin module must expose a ``register_plugin`` callable. The callable can
    accept either ``(app, api_blueprint, config)`` or ``(app, config)``. The helper
    will attempt the 3-argument signature first to allow plugins to attach their
    own API routes directly to the existing blueprint.
    """

    loaded_plugins: List[str] = []
    failed_plugins: List[str] = []

    for plugin_entry in plugin_entries or []:
        module_path, plugin_conf = _extract_plugin_fields(plugin_entry)

        if not module_path:
            logging.warning("Plugin entry is missing a module path; skipping.")
            failed_plugins.append(str(plugin_entry))
            continue

        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.error("Failed to import plugin '%s': %s", module_path, exc)
            failed_plugins.append(module_path)
            continue

        register = getattr(module, 'register_plugin', None)
        if not callable(register):
            logging.warning("Plugin '%s' does not expose register_plugin; skipping.", module_path)
            failed_plugins.append(module_path)
            continue

        try:
            signature = inspect.signature(register)
            if len(signature.parameters) >= 3:
                register(app, api_blueprint, plugin_conf)
            else:
                register(app, plugin_conf)
            loaded_plugins.append(module_path)
            logging.info("Loaded plugin '%s'", module_path)
        except TypeError:
            # If signature inspection misjudged, fall back to 2-argument call.
            try:
                register(app, plugin_conf)
                loaded_plugins.append(module_path)
                logging.info("Loaded plugin '%s'", module_path)
            except Exception as exc:  # pragma: no cover - defensive logging
                logging.error("Plugin '%s' failed to register: %s", module_path, exc)
                failed_plugins.append(module_path)
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.error("Plugin '%s' failed to register: %s", module_path, exc)
            failed_plugins.append(module_path)

    app.config['loaded_plugins'] = loaded_plugins
    app.config['failed_plugins'] = failed_plugins
    return loaded_plugins

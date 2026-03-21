"""Webhook dispatch helpers for Amps events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable


def _post_json(url: str, payload: Dict[str, Any], secret: str | None = None):
    body = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'amps-webhook',
    }
    if secret:
        signature = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
        headers['X-Amps-Signature'] = signature

    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(request, timeout=10):
        return


def dispatch_event(webhooks: Iterable[Dict[str, Any]], event: str, payload: Dict[str, Any]):
    envelope = {
        'event': event,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'payload': payload,
    }

    for hook in webhooks or []:
        if not isinstance(hook, dict):
            continue
        configured_events = hook.get('events') or ['*']
        if '*' not in configured_events and event not in configured_events:
            continue
        url = hook.get('url')
        if not url:
            continue

        def _send(target_url=url, secret=hook.get('secret')):
            try:
                _post_json(target_url, envelope, secret=secret)
            except Exception as exc:  # pragma: no cover - network dependent
                logging.error('Webhook delivery failed for %s to %s: %s', event, target_url, exc)

        threading.Thread(target=_send, daemon=True).start()

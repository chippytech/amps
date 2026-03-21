"""Authentication helpers for shared tokens and role-based API keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from flask import Request, abort, g

ROLE_ORDER = {
    'viewer': 10,
    'operator': 20,
    'admin': 30,
}


@dataclass
class AuthContext:
    key_id: str
    role: str
    scheme: str


def _role_value(role: Optional[str]) -> int:
    return ROLE_ORDER.get((role or 'viewer').lower(), 0)


def _extract_token(req: Request) -> Optional[str]:
    auth_header = req.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        return auth_header[7:].strip()
    return req.headers.get('X-Amps-Token') or req.args.get('token')


def authenticate_request(req: Request, config: Dict) -> Optional[AuthContext]:
    auth_conf = config.get('auth', {}) or {}
    if not auth_conf.get('enabled'):
        ctx = AuthContext(key_id='anonymous', role='admin', scheme='disabled')
        g.auth_context = ctx
        return ctx

    token = _extract_token(req)
    if not token:
        return None

    shared_token = auth_conf.get('token')
    if shared_token and token == shared_token:
        ctx = AuthContext(key_id='shared-token', role='admin', scheme='shared_token')
        g.auth_context = ctx
        return ctx

    for entry in auth_conf.get('api_keys', []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get('key') != token:
            continue
        role = (entry.get('role') or 'viewer').lower()
        ctx = AuthContext(
            key_id=entry.get('name') or entry.get('id') or 'api-key',
            role=role if role in ROLE_ORDER else 'viewer',
            scheme='api_key',
        )
        g.auth_context = ctx
        return ctx

    return None


def ensure_request_auth(req: Request, config: Dict, min_role: str = 'viewer') -> AuthContext:
    ctx = authenticate_request(req, config)
    if not ctx:
        abort(401, description='Unauthorized: valid token or API key required.')
    if _role_value(ctx.role) < _role_value(min_role):
        abort(403, description=f'Forbidden: {min_role} role required.')
    g.auth_context = ctx
    return ctx


def current_auth() -> Optional[AuthContext]:
    return getattr(g, 'auth_context', None)

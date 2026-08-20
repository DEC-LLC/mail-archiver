"""Microsoft OAuth2 for Mail Archiver.

Implements the OAuth2 Authorization Code flow for Microsoft 365 / Outlook.com
IMAP access. Each user registers their own Azure AD app (free) and provides
the client ID + secret.

Since v1.0.8 the OAuth "config" is a set of NAMED OAuth apps at two scopes:
  * server-wide  — /opt/mail-archiver/oauth2_config.json (shared by everyone)
  * per-user     — ${DATA}/${user}/.config/oauth_apps.json (private)

Each account may pin itself to a specific app via account['oauth_app_id'];
otherwise it resolves to the per-user default for its provider, then the
server default. See resolve_oauth_app() below.

Legacy schema (v1) — `{"microsoft": {"client_id": "...", "client_secret": "..."}}`
— is auto-migrated on first load to v2 with the app named `srv-microsoft-default`
and set as the server default for provider=microsoft.
"""

import json
import os
import time
import secrets
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError


# Scopes for IMAP access — provider-fixed for Microsoft.
#
# CRITICAL: use `outlook.office.com` (NOT `outlook.office365.com`).
# The `.office365.com` variant is work/school-only — Azure rejects it with
# "The provided resource value for the input parameter 'scope' is not valid"
# when a personal Microsoft account (hotmail.com/outlook.com/live.com) tries
# to authorize. The `.office.com` variant is the unified endpoint that
# accepts BOTH personal MSA + work/school accounts, so it's the correct
# default for a Mail Archiver serving mixed account types via tenant=common.
SCOPES = [
    'https://outlook.office.com/IMAP.AccessAsUser.All',
    'offline_access',
    'openid',
    'email',
]

# The set of scopes we accept as "server-scope" vs "user-scope"
SCOPE_SERVER = 'server'
SCOPE_USER = 'user'
KNOWN_PROVIDERS = ('microsoft',)


# ---------------------------------------------------------------------------
# MicrosoftOAuth2 — the actual OAuth client. Now tenant-aware.
# ---------------------------------------------------------------------------

class MicrosoftOAuth2:
    """OAuth2 authorization-code flow for Microsoft 365 / Outlook IMAP.

    tenant defaults to 'common' — accepts personal MSAs + any org tenant. Set
    to a specific tenant GUID or verified domain for single-tenant apps.
    """

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 tenant: str = 'common'):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.tenant = tenant or 'common'
        self._authority = f'https://login.microsoftonline.com/{self.tenant}'
        self._authorize_url = f'{self._authority}/oauth2/v2.0/authorize'
        self._token_url = f'{self._authority}/oauth2/v2.0/token'

    def get_authorization_url(self, state: str = None) -> tuple:
        if state is None:
            state = secrets.token_urlsafe(32)
        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'redirect_uri': self.redirect_uri,
            'scope': ' '.join(SCOPES),
            'state': state,
            'response_mode': 'query',
            'prompt': 'consent',
        }
        return f'{self._authorize_url}?{urlencode(params)}', state

    def exchange_code(self, code: str) -> dict:
        data = urlencode({
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': code,
            'redirect_uri': self.redirect_uri,
            'grant_type': 'authorization_code',
            'scope': ' '.join(SCOPES),
        }).encode()
        req = Request(self._token_url, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except URLError as e:
            raise ValueError(f'Token exchange failed: {e}')
        if 'error' in result:
            raise ValueError(f'OAuth2 error: {result.get("error_description", result["error"])}')
        return result

    def refresh_access_token(self, refresh_token: str) -> dict:
        data = urlencode({
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token',
            'scope': ' '.join(SCOPES),
        }).encode()
        req = Request(self._token_url, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except URLError as e:
            raise ValueError(f'Token refresh failed: {e}')
        if 'error' in result:
            raise ValueError(f'OAuth2 refresh error: {result.get("error_description", result["error"])}')
        return result


# ---------------------------------------------------------------------------
# Per-account token storage (unchanged from v1.0.7)
# ---------------------------------------------------------------------------

def save_oauth2_tokens(data_dir: str, username: str, email: str, tokens: dict):
    """Save OAuth2 tokens for an account. Chowns to target PAM user."""
    import pwd
    config_dir = Path(data_dir) / username / '.config'
    config_dir.mkdir(parents=True, exist_ok=True)

    target_uid = target_gid = None
    try:
        pw = pwd.getpwnam(username)
        target_uid, target_gid = pw.pw_uid, pw.pw_gid
        try:
            os.chown(config_dir, target_uid, target_gid)
        except (PermissionError, OSError):
            pass
    except KeyError:
        pass

    safe_name = email.replace('@', '_at_').replace('.', '_')
    token_file = config_dir / f'{safe_name}.oauth2.json'
    token_data = {
        'access_token': tokens.get('access_token', ''),
        'refresh_token': tokens.get('refresh_token', ''),
        'expires_at': int(time.time()) + int(tokens.get('expires_in', 3600)),
        'scope': tokens.get('scope', ''),
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(token_file, 'w') as f:
        json.dump(token_data, f, indent=2)
    token_file.chmod(0o600)
    if target_uid is not None:
        try:
            os.chown(token_file, target_uid, target_gid)
        except (PermissionError, OSError):
            pass

    pass_file = config_dir / f'{safe_name}.token'
    with open(pass_file, 'w') as f:
        f.write(tokens['access_token'])
    pass_file.chmod(0o600)
    if target_uid is not None:
        try:
            os.chown(pass_file, target_uid, target_gid)
        except (PermissionError, OSError):
            pass


def load_oauth2_tokens(data_dir: str, username: str, email: str) -> dict:
    config_dir = Path(data_dir) / username / '.config'
    safe_name = email.replace('@', '_at_').replace('.', '_')
    token_file = config_dir / f'{safe_name}.oauth2.json'
    if not token_file.exists():
        return {}
    try:
        with open(token_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def is_token_expired(tokens: dict, buffer_seconds: int = 300) -> bool:
    return time.time() >= (tokens.get('expires_at', 0) - buffer_seconds)


def ensure_fresh_token(oauth2: MicrosoftOAuth2, data_dir: str,
                       username: str, email: str) -> str:
    tokens = load_oauth2_tokens(data_dir, username, email)
    if not tokens or not tokens.get('refresh_token'):
        raise ValueError(f'No OAuth2 tokens for {email}. Re-authenticate via the web UI.')
    if is_token_expired(tokens):
        new_tokens = oauth2.refresh_access_token(tokens['refresh_token'])
        if 'refresh_token' not in new_tokens:
            new_tokens['refresh_token'] = tokens['refresh_token']
        save_oauth2_tokens(data_dir, username, email, new_tokens)
        return new_tokens['access_token']
    return tokens['access_token']


# ---------------------------------------------------------------------------
# NAMED OAUTH APPS — v2 schema, server + per-user scopes
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2


def _empty_config() -> dict:
    return {'_schema_version': SCHEMA_VERSION, 'apps': {}, 'defaults': {}}


def _migrate_v1(cfg: dict, scope: str) -> tuple:
    """Migrate a v1 config to v2. Returns (v2_cfg, changed_bool).

    v1: {"microsoft": {"client_id": "...", "client_secret": "..."}}
    v2: {"_schema_version": 2, "apps": {...}, "defaults": {...}}
    """
    if cfg.get('_schema_version') == SCHEMA_VERSION:
        return cfg, False

    v2 = _empty_config()
    prefix = 'srv-' if scope == SCOPE_SERVER else 'usr-'

    for provider in KNOWN_PROVIDERS:
        legacy = cfg.get(provider)
        if not (isinstance(legacy, dict) and legacy.get('client_id')):
            continue
        app_id = f'{prefix}{provider}-default'
        v2['apps'][app_id] = {
            'scope': scope,
            'provider': provider,
            'name': f'{provider.title()} (migrated from v1)',
            'client_id': legacy.get('client_id', ''),
            'client_secret': legacy.get('client_secret', ''),
            'tenant': legacy.get('tenant', 'common'),
        }
        v2['defaults'][provider] = app_id

    return v2, True


# ---- Server-scope config -----------------------------------------------------

def _server_config_path(data_dir: str) -> Path:
    return Path(data_dir) / '.oauth2_config.json'


def load_oauth2_config(data_dir: str) -> dict:
    """Load server-wide OAuth apps config. Auto-migrates v1 → v2 on read."""
    path = _server_config_path(data_dir)
    if not path.exists():
        return _empty_config()
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_config()

    migrated, changed = _migrate_v1(cfg, SCOPE_SERVER)
    if changed:
        try:
            save_oauth2_config(data_dir, migrated)
        except OSError:
            pass  # non-fatal; runtime still uses migrated in-memory
    return migrated


def save_oauth2_config(data_dir: str, config: dict):
    """Save server-wide OAuth apps config."""
    path = _server_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Always stamp schema version
    config.setdefault('_schema_version', SCHEMA_VERSION)
    with open(path, 'w') as f:
        json.dump(config, f, indent=2)
    path.chmod(0o600)


# ---- Per-user-scope config --------------------------------------------------

def _user_config_path(data_dir: str, username: str) -> Path:
    return Path(data_dir) / username / '.config' / 'oauth_apps.json'


def load_user_oauth2_config(data_dir: str, username: str) -> dict:
    """Load a user's private OAuth apps. Never migrates from v1 (no legacy shape
    ever existed at this path — v1 was server-scope only)."""
    path = _user_config_path(data_dir, username)
    if not path.exists():
        return _empty_config()
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_config()
    cfg.setdefault('_schema_version', SCHEMA_VERSION)
    cfg.setdefault('apps', {})
    cfg.setdefault('defaults', {})
    return cfg


def save_user_oauth2_config(data_dir: str, username: str, config: dict):
    """Save a user's private OAuth apps."""
    import pwd
    path = _user_config_path(data_dir, username)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.setdefault('_schema_version', SCHEMA_VERSION)
    with open(path, 'w') as f:
        json.dump(config, f, indent=2)
    path.chmod(0o600)
    # Chown to the PAM user so mbsync-as-user could read it if we ever moved
    # PassCmd lookups to per-user context. Best-effort.
    try:
        pw = pwd.getpwnam(username)
        try:
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except (PermissionError, OSError):
            pass
    except KeyError:
        pass


# ---- Unified list + resolve --------------------------------------------------

def list_oauth_apps(data_dir: str, username: str = None,
                    provider: str = None) -> list:
    """List all OAuth apps visible to a user. Returns a list of dicts:
        {'scope', 'app_id', 'name', 'provider', 'tenant', 'client_id',
         'client_secret', 'is_default'}
    Filtered by provider if given. When username is None, only server apps.
    """
    server = load_oauth2_config(data_dir)
    user = load_user_oauth2_config(data_dir, username) if username else _empty_config()

    out = []
    for scope, cfg in ((SCOPE_SERVER, server), (SCOPE_USER, user)):
        defaults = cfg.get('defaults', {}) or {}
        for app_id, app in (cfg.get('apps', {}) or {}).items():
            if provider and app.get('provider') != provider:
                continue
            out.append({
                'scope': scope,
                'app_id': app_id,
                'name': app.get('name', app_id),
                'provider': app.get('provider', ''),
                'tenant': app.get('tenant', 'common'),
                'client_id': app.get('client_id', ''),
                'client_secret': app.get('client_secret', ''),
                'is_default': defaults.get(app.get('provider')) == app_id,
            })
    return out


def _find_in(cfg: dict, app_id: str) -> dict:
    """Return the app dict for app_id in this cfg, or None."""
    return (cfg.get('apps') or {}).get(app_id)


def resolve_oauth_app(data_dir: str, username: str,
                      app_id: str = None, provider: str = None) -> dict:
    """Resolve which OAuth app to use.

    Precedence:
      1. Explicit app_id — looked up in server or user cfg by its `srv-`/`usr-` prefix
      2. User default for provider (if provider given + user has one)
      3. Server default for provider (if provider given + server has one)
      4. None

    Returns the app dict with an added 'app_id' + 'scope' key, or None.
    """
    server = load_oauth2_config(data_dir)
    user = load_user_oauth2_config(data_dir, username) if username else _empty_config()

    # 1. Explicit
    if app_id:
        if app_id.startswith('usr-'):
            app = _find_in(user, app_id)
            if app:
                return dict(app, app_id=app_id, scope=SCOPE_USER)
        else:
            app = _find_in(server, app_id)
            if app:
                return dict(app, app_id=app_id, scope=SCOPE_SERVER)
        # Explicit but missing — fall through to defaults so a deleted app
        # doesn't hard-break an existing account; caller can decide to warn.

    if not provider:
        return None

    # 2. User default for this provider
    u_default_id = (user.get('defaults') or {}).get(provider)
    if u_default_id:
        app = _find_in(user, u_default_id)
        if app:
            return dict(app, app_id=u_default_id, scope=SCOPE_USER)

    # 3. Server default for this provider
    s_default_id = (server.get('defaults') or {}).get(provider)
    if s_default_id:
        app = _find_in(server, s_default_id)
        if app:
            return dict(app, app_id=s_default_id, scope=SCOPE_SERVER)

    return None


def build_microsoft_oauth2(app: dict, redirect_uri: str) -> MicrosoftOAuth2:
    """Build a MicrosoftOAuth2 instance from a resolved app dict."""
    if not app:
        raise ValueError('No Microsoft OAuth2 app configured. '
                         'Go to Server Settings → OAuth Apps to add one.')
    cid = app.get('client_id', '')
    csec = app.get('client_secret', '')
    if not cid or not csec:
        raise ValueError(f'OAuth app "{app.get("name", "?")}" is missing client_id or client_secret.')
    return MicrosoftOAuth2(cid, csec, redirect_uri, tenant=app.get('tenant', 'common'))


# ---------------------------------------------------------------------------
# Backward-compat shim: old callers of get_microsoft_oauth2(data_dir, redir)
# ---------------------------------------------------------------------------

def get_microsoft_oauth2(data_dir: str, redirect_uri: str,
                         username: str = None, app_id: str = None) -> MicrosoftOAuth2:
    """Backward-compat wrapper. Resolves provider=microsoft with the standard
    precedence and returns a ready-to-use MicrosoftOAuth2."""
    app = resolve_oauth_app(data_dir, username, app_id=app_id, provider='microsoft')
    if not app:
        raise ValueError(
            'Microsoft OAuth2 not configured. '
            'Go to Server Settings → OAuth Apps to add one.'
        )
    return build_microsoft_oauth2(app, redirect_uri)


# ---------------------------------------------------------------------------
# App-CRUD helpers used by the /oauth2/apps/* routes
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    import re
    s = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return s or 'app'


def new_app_id(scope: str, provider: str, name: str, taken: set) -> str:
    """Generate a fresh unique app_id."""
    prefix = 'srv-' if scope == SCOPE_SERVER else 'usr-'
    base = f'{prefix}{provider}-{_slugify(name)}'
    if base not in taken:
        return base
    for i in range(2, 1000):
        cand = f'{base}-{i}'
        if cand not in taken:
            return cand
    # Absurd fallback
    return f'{base}-{secrets.token_hex(4)}'


def set_default_app(cfg: dict, app_id: str) -> dict:
    """Mark app_id as the default for its provider within cfg. Returns cfg."""
    app = (cfg.get('apps') or {}).get(app_id)
    if not app:
        return cfg
    cfg.setdefault('defaults', {})[app.get('provider')] = app_id
    return cfg


def clear_default_if(cfg: dict, app_id: str) -> dict:
    """Remove app_id from defaults if it was set. Returns cfg."""
    defs = cfg.get('defaults') or {}
    for provider, did in list(defs.items()):
        if did == app_id:
            del defs[provider]
    return cfg

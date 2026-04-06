"""Microsoft OAuth2 for Mail Archiver.

Implements the OAuth2 Authorization Code flow for Microsoft 365 / Outlook.com
IMAP access. The user registers their own Azure AD app (free) and provides
the client ID + secret. No DEC-LLC credentials baked in.

Flow:
  1. User clicks "Add Outlook Account" on the web UI
  2. App redirects to Microsoft login (authorization URL)
  3. User authenticates and grants IMAP access
  4. Microsoft redirects back with an authorization code
  5. App exchanges the code for access + refresh tokens
  6. Tokens are stored per-account, refresh token used for renewals
  7. mbsync uses the access token via XOAUTH2

Azure AD App Setup (user does this once):
  1. Go to https://portal.azure.com → Azure Active Directory → App Registrations
  2. New Registration: name="Mail Archiver", redirect URI=http://localhost:8400/oauth2/callback
  3. API Permissions: add "Microsoft Graph → IMAP.AccessAsUser.All" (delegated)
  4. Certificates & Secrets: create a client secret
  5. Copy Application (client) ID + client secret into Mail Archiver settings

Scopes required:
  - https://outlook.office365.com/IMAP.AccessAsUser.All (IMAP access)
  - offline_access (refresh tokens)
  - openid (user identity)
"""

import json
import time
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError

# Microsoft OAuth2 endpoints
AUTHORITY = 'https://login.microsoftonline.com/common'
AUTHORIZE_URL = f'{AUTHORITY}/oauth2/v2.0/authorize'
TOKEN_URL = f'{AUTHORITY}/oauth2/v2.0/token'

# Scopes for IMAP access
SCOPES = [
    'https://outlook.office365.com/IMAP.AccessAsUser.All',
    'offline_access',
    'openid',
    'email',
]


class MicrosoftOAuth2:
    """Manages OAuth2 tokens for Microsoft 365 / Outlook IMAP access."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def get_authorization_url(self, state: str = None) -> tuple:
        """Generate the Microsoft login URL.

        Returns (url, state) — redirect the user to url, then verify state
        in the callback.
        """
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
        url = f'{AUTHORIZE_URL}?{urlencode(params)}'
        return url, state

    def exchange_code(self, code: str) -> dict:
        """Exchange authorization code for access + refresh tokens.

        Returns dict with: access_token, refresh_token, expires_in, token_type, scope
        Raises ValueError on failure.
        """
        data = urlencode({
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': code,
            'redirect_uri': self.redirect_uri,
            'grant_type': 'authorization_code',
            'scope': ' '.join(SCOPES),
        }).encode()

        req = Request(TOKEN_URL, data=data, method='POST')
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
        """Use refresh token to get a new access token.

        Returns dict with: access_token, refresh_token (possibly new), expires_in
        Raises ValueError on failure.
        """
        data = urlencode({
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token',
            'scope': ' '.join(SCOPES),
        }).encode()

        req = Request(TOKEN_URL, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except URLError as e:
            raise ValueError(f'Token refresh failed: {e}')

        if 'error' in result:
            raise ValueError(f'OAuth2 refresh error: {result.get("error_description", result["error"])}')

        return result


def save_oauth2_tokens(data_dir: str, username: str, email: str, tokens: dict):
    """Save OAuth2 tokens for an account."""
    config_dir = Path(data_dir) / username / '.config'
    config_dir.mkdir(parents=True, exist_ok=True)

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

    # Also write the access token to the .token file that mbsync reads
    # mbsync's PassCmd reads this file for XOAUTH2
    pass_file = config_dir / f'{safe_name}.token'
    with open(pass_file, 'w') as f:
        f.write(tokens['access_token'])
    pass_file.chmod(0o600)


def load_oauth2_tokens(data_dir: str, username: str, email: str) -> dict:
    """Load OAuth2 tokens for an account. Returns empty dict if not found."""
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
    """Check if the access token is expired or about to expire."""
    expires_at = tokens.get('expires_at', 0)
    return time.time() >= (expires_at - buffer_seconds)


def ensure_fresh_token(oauth2: MicrosoftOAuth2, data_dir: str,
                       username: str, email: str) -> str:
    """Ensure the access token is fresh. Refresh if needed. Returns access_token.

    Call this before every mbsync run for OAuth2 accounts.
    """
    tokens = load_oauth2_tokens(data_dir, username, email)
    if not tokens or not tokens.get('refresh_token'):
        raise ValueError(f'No OAuth2 tokens for {email}. Re-authenticate via the web UI.')

    if is_token_expired(tokens):
        # Refresh the token
        new_tokens = oauth2.refresh_access_token(tokens['refresh_token'])
        # Microsoft may return a new refresh token
        if 'refresh_token' not in new_tokens:
            new_tokens['refresh_token'] = tokens['refresh_token']
        save_oauth2_tokens(data_dir, username, email, new_tokens)
        return new_tokens['access_token']

    return tokens['access_token']


# --- OAuth2 configuration management ---

def load_oauth2_config(data_dir: str) -> dict:
    """Load global OAuth2 client configuration.

    Stored at $data_dir/.oauth2_config.json:
    {
        "microsoft": {
            "client_id": "...",
            "client_secret": "..."
        }
    }
    """
    config_file = Path(data_dir) / '.oauth2_config.json'
    if config_file.exists():
        try:
            with open(config_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_oauth2_config(data_dir: str, config: dict):
    """Save global OAuth2 client configuration."""
    config_file = Path(data_dir) / '.oauth2_config.json'
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    config_file.chmod(0o600)


def get_microsoft_oauth2(data_dir: str, redirect_uri: str) -> MicrosoftOAuth2:
    """Create a MicrosoftOAuth2 instance from saved config.

    Raises ValueError if not configured.
    """
    config = load_oauth2_config(data_dir)
    ms_config = config.get('microsoft', {})

    client_id = ms_config.get('client_id', '')
    client_secret = ms_config.get('client_secret', '')

    if not client_id or not client_secret:
        raise ValueError(
            'Microsoft OAuth2 not configured. '
            'Go to Settings → Microsoft OAuth2 to enter your Azure AD app credentials.'
        )

    return MicrosoftOAuth2(client_id, client_secret, redirect_uri)

"""Mail Archiver — Windows Desktop Application.

Launches a local Flask web server and opens the browser. Provides a
system tray icon for background sync scheduling.

Uses imaplib (stdlib) for IMAP sync — no mbsync dependency.
Uses SQLite FTS5 for search — no external database.
"""

import os
import sys
import webbrowser
import threading
import time
import logging
from pathlib import Path

# Determine data directory
if os.name == 'nt':
    DATA_DIR = Path(os.environ.get('APPDATA', os.path.expanduser('~'))) / 'MailArchiver'
else:
    DATA_DIR = Path.home() / '.mail-archiver'

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = DATA_DIR / 'mail-archiver.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger('mail_archiver')

# Set env vars for the Flask app
os.environ['MAIL_ARCHIVER_DATA'] = str(DATA_DIR)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ['MAIL_ARCHIVER_PORT'] = '8400'

# Secret key persistence
secret_file = DATA_DIR / '.secret_key'
if not secret_file.exists():
    import secrets
    secret_file.write_text(secrets.token_hex(32))
os.environ['MAIL_ARCHIVER_SECRET_FILE'] = str(secret_file)

PORT = 8400
URL = f'http://127.0.0.1:{PORT}'


def run_flask():
    """Run the Flask web server in a background thread."""
    # Import here so env vars are set first
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import app
    app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False)


def run_tray():
    """Run system tray icon (Windows only, uses pystray if available)."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log.info('pystray/PIL not available — running without system tray icon')
        return

    # Create a simple icon
    icon_img = Image.new('RGB', (64, 64), color=(33, 150, 243))

    def on_open(icon, item):
        webbrowser.open(URL)

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem('Open Mail Archiver', on_open, default=True),
        pystray.MenuItem('Quit', on_quit),
    )

    icon = pystray.Icon('mail-archiver', icon_img, 'Mail Archiver', menu)
    icon.run()


def main():
    """Main entry point for the Windows desktop app."""
    log.info('Starting Mail Archiver')
    log.info('Data directory: %s', DATA_DIR)

    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Wait for Flask to start
    time.sleep(1.5)

    # Open browser
    log.info('Opening browser at %s', URL)
    webbrowser.open(URL)

    # Try system tray (blocking if available, otherwise just wait)
    try:
        run_tray()
    except Exception:
        # No tray — just keep running
        log.info('Running in console mode (close this window to stop)')
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass

    log.info('Mail Archiver stopped')


if __name__ == '__main__':
    main()

#!/bin/bash
# Deploy mail-archiver to a target host.
# Usage: ./deploy.sh user@hostname [data_dir]
# Example: ./deploy.sh root@pnas-omv5s.decllc.biz /datapool/email-archive

set -euo pipefail

TARGET="${1:?Usage: ./deploy.sh user@hostname [data_dir]}"
DATA_DIR="${2:-/datapool/email-archive}"
INSTALL_DIR="/opt/mail-archiver"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying mail-archiver to $TARGET ==="

# Copy app files (all Python modules + templates + static)
echo "Copying application files..."
ssh "$TARGET" "mkdir -p $INSTALL_DIR/templates $INSTALL_DIR/static"
scp "$SCRIPT_DIR/app.py" "$SCRIPT_DIR/search_index.py" "$SCRIPT_DIR/oauth2_microsoft.py" \
    "$TARGET:$INSTALL_DIR/"
scp "$SCRIPT_DIR/templates/"*.html "$TARGET:$INSTALL_DIR/templates/"
scp "$SCRIPT_DIR/static/"*.css "$TARGET:$INSTALL_DIR/static/"

# Generate secret key if not present
echo "Checking secret key..."
ssh "$TARGET" "test -f $INSTALL_DIR/.secret_key || ( python3 -c 'import secrets; print(secrets.token_hex(32))' > $INSTALL_DIR/.secret_key && chmod 600 $INSTALL_DIR/.secret_key )"

# Install systemd service
echo "Installing systemd service..."
scp "$SCRIPT_DIR/mail-archiver.service" "$TARGET:/etc/systemd/system/mail-archiver.service"
ssh "$TARGET" "sed -i 's|MAIL_ARCHIVER_DATA=.*|MAIL_ARCHIVER_DATA=$DATA_DIR|' /etc/systemd/system/mail-archiver.service"
ssh "$TARGET" "systemctl daemon-reload && systemctl enable mail-archiver && systemctl restart mail-archiver"

# Install hourly scheduled sync cron
echo "Installing scheduled sync cron..."
ssh "$TARGET" "cat > /etc/cron.d/mail-archiver << CRON
# Hourly scheduled sync — respects per-account intervals, auto-staggered
MAIL_ARCHIVER_DATA=$DATA_DIR
0 * * * * root cd $INSTALL_DIR && python3 app.py scheduled-sync >> /var/log/mail-archiver-sync.log 2>&1
CRON"

echo ""
echo "=== Checking service status ==="
ssh "$TARGET" "systemctl status mail-archiver --no-pager | head -10"

echo ""
echo "=== Deployment complete ==="
echo "Access: http://$(echo "$TARGET" | cut -d@ -f2):8400/"
echo "Scheduled sync: hourly check via /etc/cron.d/mail-archiver (per-account intervals)"

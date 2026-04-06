#!/bin/bash
# Build RPM and DEB packages for mail-archiver
# Run from the mail-archiver/ directory
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(dirname "$SCRIPT_DIR")"
VERSION="1.0.0"
RELEASE="1"
NAME="mail-archiver"

echo "=== Building mail-archiver packages ==="
echo "Source: $SRC_DIR"
echo "Version: $VERSION-$RELEASE"

# ============================================================
# RPM BUILD
# ============================================================
echo ""
echo "=== Building RPM ==="

RPMBUILD_DIR=$(mktemp -d)
mkdir -p "$RPMBUILD_DIR"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}

# Copy source files to SOURCES
cp "$SRC_DIR/app.py" "$RPMBUILD_DIR/SOURCES/"
cp "$SRC_DIR/search_index.py" "$RPMBUILD_DIR/SOURCES/"
cp "$SRC_DIR/oauth2_microsoft.py" "$RPMBUILD_DIR/SOURCES/"
cp -r "$SRC_DIR/templates" "$RPMBUILD_DIR/SOURCES/"
cp -r "$SRC_DIR/static" "$RPMBUILD_DIR/SOURCES/"
cp "$SRC_DIR/mail-archiver.service" "$RPMBUILD_DIR/SOURCES/"

# Copy spec
cp "$SCRIPT_DIR/rpm/mail-archiver.spec" "$RPMBUILD_DIR/SPECS/"

# Build
rpmbuild --define "_topdir $RPMBUILD_DIR" -bb "$RPMBUILD_DIR/SPECS/mail-archiver.spec" 2>&1

# Copy result
RPM_FILE=$(find "$RPMBUILD_DIR/RPMS" -name "*.rpm" | head -1)
if [ -n "$RPM_FILE" ]; then
    cp "$RPM_FILE" "$SCRIPT_DIR/"
    echo "RPM: $SCRIPT_DIR/$(basename "$RPM_FILE")"
    rpm -qpi "$RPM_FILE" | head -5
    echo "Files:"
    rpm -qpl "$RPM_FILE"
fi
rm -rf "$RPMBUILD_DIR"

# ============================================================
# DEB BUILD
# ============================================================
echo ""
echo "=== Building DEB ==="

DEB_DIR=$(mktemp -d)
DEB_PKG="$DEB_DIR/${NAME}_${VERSION}-${RELEASE}_all"
mkdir -p "$DEB_PKG/opt/mail-archiver/templates"
mkdir -p "$DEB_PKG/opt/mail-archiver/static"
mkdir -p "$DEB_PKG/etc/systemd/system"
mkdir -p "$DEB_PKG/etc/cron.d"
mkdir -p "$DEB_PKG/DEBIAN"

# Copy application files
cp "$SRC_DIR/app.py" "$DEB_PKG/opt/mail-archiver/"
cp "$SRC_DIR/search_index.py" "$DEB_PKG/opt/mail-archiver/"
cp "$SRC_DIR/oauth2_microsoft.py" "$DEB_PKG/opt/mail-archiver/"
cp "$SRC_DIR/templates/"*.html "$DEB_PKG/opt/mail-archiver/templates/"
cp "$SRC_DIR/static/"*.css "$DEB_PKG/opt/mail-archiver/static/"
cp "$SRC_DIR/mail-archiver.service" "$DEB_PKG/etc/systemd/system/"

# Cron job
cat > "$DEB_PKG/etc/cron.d/mail-archiver" << 'CRON'
# Hourly scheduled sync — respects per-account intervals, auto-staggered
0 * * * * root cd /opt/mail-archiver && python3 app.py scheduled-sync >> /var/log/mail-archiver-sync.log 2>&1
CRON

# DEBIAN control files
cp "$SCRIPT_DIR/deb/DEBIAN/control" "$DEB_PKG/DEBIAN/"
cp "$SCRIPT_DIR/deb/DEBIAN/postinst" "$DEB_PKG/DEBIAN/"
chmod 755 "$DEB_PKG/DEBIAN/postinst"

# Build DEB
dpkg-deb --build "$DEB_PKG" "$SCRIPT_DIR/${NAME}_${VERSION}-${RELEASE}_all.deb" 2>&1

DEB_FILE="$SCRIPT_DIR/${NAME}_${VERSION}-${RELEASE}_all.deb"
if [ -f "$DEB_FILE" ]; then
    echo "DEB: $DEB_FILE"
    dpkg-deb --info "$DEB_FILE" | head -10
fi
rm -rf "$DEB_DIR"

echo ""
echo "=== Build complete ==="
ls -lh "$SCRIPT_DIR"/*.rpm "$SCRIPT_DIR"/*.deb 2>/dev/null

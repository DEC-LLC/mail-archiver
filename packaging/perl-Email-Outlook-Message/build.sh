#!/bin/bash
# Build perl-Email-Outlook-Message (rpm + srpm) for EL9 in a throwaway
# Rocky 9 container. Everything comes from dnf (BaseOS, AppStream, CRB,
# EPEL); nothing is installed from CPAN and nothing on the host changes.
#
#   packaging/perl-Email-Outlook-Message/build.sh <output-dir>
#
# The upstream tarball is downloaded from CPAN and must match the pinned
# sha256, or the build stops. To build without network access to CPAN, put
# Email-Outlook-Message-<version>.tar.gz next to this script (same check).
set -euo pipefail
umask 022

NAME="perl-Email-Outlook-Message"
VERSION="0.921"
TARBALL="Email-Outlook-Message-${VERSION}.tar.gz"
SOURCE_URL="https://www.cpan.org/authors/id/M/MV/MVZ/${TARBALL}"
SOURCE_SHA256="fb4abeea14cda51c1e60bc211cf9521bbbaae74b84118ca6d58f0a7223680388"
IMAGE="${BUILD_IMAGE:-docker.io/rockylinux/rockylinux:9}"

[ $# -eq 1 ] || { echo "usage: $0 <output-dir>" >&2; exit 2; }
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$1"
OUT_DIR="$(cd "$1" && pwd)"
command -v podman >/dev/null || { echo "ERROR: podman not found" >&2; exit 1; }
[ -f "$SCRIPT_DIR/$NAME.spec" ] || { echo "ERROR: $NAME.spec missing" >&2; exit 1; }

echo "=== $NAME $VERSION: building in $IMAGE ==="
echo "Output: $OUT_DIR"

podman run --rm -i \
    -v "$SCRIPT_DIR:/src:ro,Z" \
    -v "$OUT_DIR:/out:Z" \
    -e NAME="$NAME" -e TARBALL="$TARBALL" \
    -e SOURCE_URL="$SOURCE_URL" -e SOURCE_SHA256="$SOURCE_SHA256" \
    "$IMAGE" /bin/bash -euo pipefail -s <<'CONTAINER'
umask 022
echo "--- repositories: EPEL + CRB"
dnf -y -q install epel-release dnf-plugins-core
dnf config-manager --set-enabled crb
echo "--- build tools"
dnf -y -q install rpm-build

TOP=/root/rpmbuild
mkdir -p "$TOP"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}
cp "/src/$NAME.spec" "$TOP/SPECS/"

echo "--- source tarball"
if [ -f "/src/$TARBALL" ]; then
    cp "/src/$TARBALL" "$TOP/SOURCES/"
else
    curl -fsSL --retry 3 -o "$TOP/SOURCES/$TARBALL" "$SOURCE_URL"
fi
GOT="$(sha256sum "$TOP/SOURCES/$TARBALL" | cut -d' ' -f1)"
if [ "$GOT" != "$SOURCE_SHA256" ]; then
    echo "ERROR: $TARBALL sha256 mismatch" >&2
    echo "  expected $SOURCE_SHA256" >&2
    echo "  got      $GOT" >&2
    exit 1
fi
echo "sha256 OK: $GOT  $TARBALL"

echo "--- build dependencies (dnf builddep)"
dnf -y -q builddep "$TOP/SPECS/$NAME.spec"

echo "--- rpmbuild -ba"
rpmbuild --define "_topdir $TOP" -ba "$TOP/SPECS/$NAME.spec"

cp "$TOP"/RPMS/noarch/*.rpm "$TOP"/SRPMS/*.src.rpm /out/
chmod 0644 /out/*.rpm
CONTAINER

RPM="$(ls "$OUT_DIR"/${NAME}-${VERSION}-*.noarch.rpm | head -1)"
SRPM="$(ls "$OUT_DIR"/${NAME}-${VERSION}-*.src.rpm | head -1)"

echo ""
echo "=== sha256 ==="
(cd "$OUT_DIR" && sha256sum "$(basename "$RPM")" "$(basename "$SRPM")")

# rpm may not exist on the build host (Debian/Ubuntu workstation): query
# inside the same image.
echo ""
podman run --rm -i -v "$OUT_DIR:/out:ro,Z" -e RPM="$(basename "$RPM")" \
    "$IMAGE" /bin/bash -euo pipefail -s <<'QUERY'
echo "=== rpm -qpl $RPM ==="
rpm -qpl "/out/$RPM"
echo ""
echo "=== rpm -qpR $RPM ==="
rpm -qpR "/out/$RPM"
QUERY

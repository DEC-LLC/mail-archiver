#!/bin/bash
# Make the source artifacts of a release from a git commit:
#   mail-archiver-<version>.tar.gz        Source0 of the rpm, .orig.tar.gz of the deb
#   MailArchiver-<version>-source.zip     the same tree, for the Windows application
# Both are a `git archive` of the commit plus one generated file, BUILD_INFO
# (commit and commit date), so a package rebuilt from them shows on its About
# page which commit it came from. The same commit always gives the same bytes.
#
# usage: packaging/make-source.sh [<commit-ish>] [<output directory>]
# Needs git; this is the only packaging step that does. Everything after it
# (packaging/build.sh rpm|deb) works from the tarball alone.
set -euo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(dirname "$SCRIPT_DIR")"
REF="${1:-HEAD}"
OUT="${2:-$SCRIPT_DIR/dist}"
NAME=mail-archiver

COMMIT="$(git -C "$SRC_DIR" rev-parse --verify "${REF}^{commit}")"
VERSION="$(git -C "$SRC_DIR" show "$COMMIT:VERSION" | tr -d '[:space:]')"
[ -n "$VERSION" ] || { echo "ERROR: no VERSION file in $COMMIT" >&2; exit 1; }
# Commit date in UTC: fixed for a commit, so the archive is reproducible.
BUILT="$(TZ=UTC git -C "$SRC_DIR" show -s --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd "$COMMIT")"
# BUILD_INFO_TEXT lets a caller supply the exact text (used to regenerate the
# sources of a release that was built before this script existed).
INFO="${BUILD_INFO_TEXT:-$(printf 'commit=%s\nbuilt=%s\n' "$COMMIT" "$BUILT")}"
case "$INFO" in *$'\n') ;; *) INFO="$INFO"$'\n' ;; esac

mkdir -p "$OUT"
TARBALL="$OUT/$NAME-$VERSION.tar.gz"
ZIP="$OUT/MailArchiver-$VERSION-source.zip"
PREFIX="$NAME-$VERSION/"

git -C "$SRC_DIR" archive --format=tar --prefix="$PREFIX" \
    --add-virtual-file="${PREFIX}BUILD_INFO:$INFO" "$COMMIT" | gzip -n -9 > "$TARBALL"
git -C "$SRC_DIR" archive --format=zip --prefix="$PREFIX" \
    --add-virtual-file="${PREFIX}BUILD_INFO:$INFO" "$COMMIT" > "$ZIP"

( cd "$OUT" && sha256sum "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256" \
            && sha256sum "$(basename "$ZIP")" > "$(basename "$ZIP").sha256" )
echo "commit : $COMMIT"
echo "version: $VERSION"
echo "tarball: $TARBALL"
echo "zip    : $ZIP"
cat "$TARBALL.sha256" "$ZIP.sha256"

#!/bin/bash
# Build the mail-archiver packages and their source packages.
#
#   packaging/build.sh                      everything, from the current commit, in containers
#   packaging/build.sh all [<commit-ish>]   the same, from another commit
#   packaging/build.sh rpm <tarball> <out>  SRPM + rpm from a source tarball  (run in an EL 9 container)
#   packaging/build.sh deb <tarball> <out>  Debian source package + deb       (run in a Debian 12 container)
#
# Every package is built from one file, mail-archiver-<version>.tar.gz, made by
# packaging/make-source.sh (a `git archive` of the commit plus BUILD_INFO).
# The `rpm` and `deb` steps need neither git nor the checkout: they are what a
# rebuild from the SRPM or from the .dsc does.
#
# Results (packaging/dist/ unless another directory is given):
#   mail-archiver-<v>.tar.gz                 source tarball (+ .sha256)
#   MailArchiver-<v>-source.zip              the same tree as a zip, for the Windows build (+ .sha256)
#   mail-archiver-<v>-1.el9.src.rpm          SRPM
#   mail-archiver-<v>-1.el9.noarch.rpm       rpm
#   mail-archiver_<v>.orig.tar.gz, mail-archiver_<v>-1.debian.tar.xz, mail-archiver_<v>-1.dsc
#                                            Debian source package
#   mail-archiver_<v>-1_all.deb              deb
#   SHA256SUMS                               sha256 of every file above
set -euo pipefail
# 1.2.0 was built under umask 002 and shipped every file group-writable;
# cron then refused /etc/cron.d/mail-archiver ("INSECURE MODE").
umask 022

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAME=mail-archiver
RELEASE=1
EL_IMAGE="${EL_IMAGE:-docker.io/rockylinux/rockylinux:9}"
DEB_IMAGE="${DEB_IMAGE:-docker.io/library/debian:12}"

die() { echo "ERROR: $*" >&2; exit 1; }

version_of_tarball() {
    local base; base="$(basename "$1")"
    case "$base" in
        "$NAME"-*.tar.gz) base="${base#"$NAME"-}"; echo "${base%.tar.gz}" ;;
        *) die "not a $NAME-<version>.tar.gz: $1" ;;
    esac
}

build_rpm() {
    local tarball out version top spec
    tarball="$(readlink -f "$1")"; out="$(readlink -f "$2")"
    [ -f "$tarball" ] || die "no such tarball: $1"
    command -v rpmbuild >/dev/null || die "rpmbuild not found (run this step in an EL 9 container with rpm-build)"
    version="$(version_of_tarball "$tarball")"
    top="$(mktemp -d)"
    mkdir -p "$top"/{SOURCES,SPECS,BUILD,RPMS,SRPMS} "$out"
    cp "$tarball" "$top/SOURCES/"
    spec="$top/SPECS/$NAME.spec"
    tar -xzOf "$tarball" "$NAME-$version/packaging/rpm/$NAME.spec" > "$spec"
    grep -qE "^Version:[[:space:]]+$version\$" "$spec" \
        || die "the spec in the tarball is not at version $version (packaging/rpm/$NAME.spec and VERSION must agree)"
    echo "=== rpmbuild -ba ($NAME $version-$RELEASE) ==="
    rpmbuild --define "_topdir $top" -ba "$spec"
    find "$top/SRPMS" "$top/RPMS" -name '*.rpm' -exec cp -v {} "$out/" \;
    rm -rf -- "$top"
}

build_deb() {
    local tarball out version work
    tarball="$(readlink -f "$1")"; out="$(readlink -f "$2")"
    [ -f "$tarball" ] || die "no such tarball: $1"
    command -v dpkg-buildpackage >/dev/null || die "dpkg-buildpackage not found (run this step in a Debian container with dpkg-dev)"
    version="$(version_of_tarball "$tarball")"
    work="$(mktemp -d)"
    mkdir -p "$out"
    cp "$tarball" "$work/${NAME}_${version}.orig.tar.gz"
    tar -xzf "$tarball" -C "$work"
    ( cd "$work/$NAME-$version"
      head -1 debian/changelog | grep -qF "($version-$RELEASE)" \
          || die "debian/changelog is not at $version-$RELEASE (it must agree with VERSION)"
      echo "=== dpkg-buildpackage ($NAME $version-$RELEASE) ==="
      # -d: the only build dependency is dpkg-dev itself; no compiler is needed.
      dpkg-buildpackage -us -uc -d --build=source,all )
    cp -v "$work/${NAME}_${version}.orig.tar.gz" "$work/${NAME}_${version}-${RELEASE}".debian.tar.* \
          "$work/${NAME}_${version}-${RELEASE}.dsc" "$work/${NAME}_${version}-${RELEASE}_all.deb" "$out/"
    rm -rf -- "$work"
}

build_all() {
    local ref="${1:-HEAD}" out="${2:-$SCRIPT_DIR/dist}" version tarball
    command -v podman >/dev/null || die "podman not found: run the rpm and deb steps yourself in their containers"
    mkdir -p "$out"; out="$(readlink -f "$out")"
    "$SCRIPT_DIR/make-source.sh" "$ref" "$out"
    version="$(git -C "$SCRIPT_DIR/.." show "$ref:VERSION" | tr -d '[:space:]')"
    tarball="$NAME-$version.tar.gz"
    # The build script itself comes out of the tarball, so what runs is what was archived.
    local run='set -e; umask 022; t=$(mktemp -d); tar -xzf "/out/$1" -C "$t"; bash "$t"/*/packaging/build.sh "$2" "/out/$1" /out'
    podman run --rm -v "$out":/out:Z "$EL_IMAGE" bash -c \
        "dnf -y -q install rpm-build tar gzip findutils >/dev/null && $run" _ "$tarball" rpm
    podman run --rm -v "$out":/out:Z "$DEB_IMAGE" bash -c \
        "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends dpkg-dev make xz-utils >/dev/null && $run" _ "$tarball" deb
    ( cd "$out" && sha256sum -- "$NAME"[-_]"$version"* MailArchiver-"$version"-source.zip 2>/dev/null \
          | grep -v '\.sha256$' > SHA256SUMS )
    echo ""
    echo "=== Build complete: $out ==="
    cat "$out/SHA256SUMS"
}

case "${1:-all}" in
    rpm) [ $# -eq 3 ] || die "usage: $0 rpm <tarball> <output directory>"; build_rpm "$2" "$3" ;;
    deb) [ $# -eq 3 ] || die "usage: $0 deb <tarball> <output directory>"; build_deb "$2" "$3" ;;
    all) build_all "${2:-HEAD}" "${3:-}" ;;
    *)   die "usage: $0 [all [<commit-ish>]] | rpm <tarball> <out> | deb <tarball> <out>" ;;
esac

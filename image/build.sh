#!/usr/bin/env bash
# Build a flashable Raspberry Pi OS Lite image with figpi baked in.
#
#   sudo image/build.sh [--base URL_OR_FILE] [--out DIR] [--cache DIR] [--no-compress]
#
# What it does:
#   1. downloads the official Raspberry Pi OS Lite (arm64) image and verifies its SHA-256
#   2. mounts the root partition by byte offset (no partition probing needed, works in containers/CI)
#   3. copies the figpi package to /opt/figpi, installs the systemd units and enables them
#   4. drops figpi.toml onto the FAT boot partition with mtools (no vfat kernel support required)
#   5. recompresses to dist/figpi-<version>-<base-date>-arm64-lite.img.xz
#
# Nothing is pip-installed: the agent is pure stdlib and runs on the OS's own python3.
# Needs root (loop devices + mount) and: curl xz losetup mount python3 mcopy sha256sum
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASE="${FIGPI_BASE_IMAGE:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
OUT="$REPO/dist"
CACHE="$HERE/cache"
COMPRESS=1
HOSTNAME_IN_IMAGE="figpi"

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --cache) CACHE="$2"; shift 2 ;;
    --no-compress) COMPRESS=0; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;32m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (loop devices and mount are needed)"
for tool in curl xz losetup mount umount python3 mcopy sha256sum; do
  command -v "$tool" >/dev/null || die "missing tool: $tool (mcopy comes from the 'mtools' package)"
done

VERSION="$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('$REPO/figpi/__init__.py').read()).group(1))")"
mkdir -p "$OUT" "$CACHE"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/figpi-build.XXXXXX")"
LOOP=""
MNT="$WORK/root"
cleanup() {
  set +e
  mountpoint -q "$MNT" && umount "$MNT"
  [ -n "$LOOP" ] && losetup -d "$LOOP"
  rm -rf "$WORK"
}
trap cleanup EXIT

# --- 1. fetch and verify the base image -------------------------------------------
if [ -f "$BASE" ]; then
  BASE_XZ="$BASE"
  log "using local base image $BASE_XZ"
else
  FINAL_URL="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "$BASE")"
  BASE_XZ="$CACHE/$(basename "$FINAL_URL")"
  if [ ! -f "$BASE_XZ" ]; then
    log "downloading $FINAL_URL"
    curl -fL --progress-bar -o "$BASE_XZ.part" "$FINAL_URL" && mv "$BASE_XZ.part" "$BASE_XZ"
  else
    log "base image already cached: $BASE_XZ"
  fi
  if [ ! -f "$BASE_XZ.sha256" ]; then
    curl -fsSL -o "$BASE_XZ.sha256" "$FINAL_URL.sha256" || log "no .sha256 published next to the image, skipping verification"
  fi
fi
if [ -f "$BASE_XZ.sha256" ]; then
  log "verifying checksum"
  (cd "$(dirname "$BASE_XZ")" && sha256sum -c --quiet "$(basename "$BASE_XZ.sha256")") || die "checksum mismatch for $BASE_XZ"
fi
BASE_DATE="$(basename "$BASE_XZ" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2}' || date +%Y-%m-%d)"

IMG="$WORK/figpi.img"
log "decompressing to $IMG"
case "$BASE_XZ" in
  *.xz) xz -dc -T0 "$BASE_XZ" > "$IMG" ;;
  *.img) cp "$BASE_XZ" "$IMG" ;;
  *) die "base must be a .img or .img.xz" ;;
esac

# --- 2. partition geometry from the MBR -----------------------------------------------
read -r BOOT_OFF BOOT_SIZE ROOT_OFF ROOT_SIZE < <(python3 - "$IMG" <<'PY'
import struct, sys
with open(sys.argv[1], "rb") as f:
    mbr = f.read(512)
assert mbr[510:512] == b"\x55\xaa", "not an MBR image"
parts = []
for i in range(4):
    e = mbr[446 + 16 * i : 462 + 16 * i]
    ptype, lba, n = e[4], struct.unpack_from("<I", e, 8)[0], struct.unpack_from("<I", e, 12)[0]
    if ptype:
        parts.append((ptype, lba * 512, n * 512))
boot = next(p for p in parts if p[0] in (0x0B, 0x0C, 0x0E))
root = next(p for p in parts if p[0] == 0x83)
print(boot[1], boot[2], root[1], root[2])
PY
)
log "boot partition at $BOOT_OFF ($((BOOT_SIZE / 1048576)) MiB), root at $ROOT_OFF ($((ROOT_SIZE / 1048576)) MiB)"

# --- 3. root filesystem: payload + units -----------------------------------------------
LOOP="$(losetup -f --show -o "$ROOT_OFF" --sizelimit "$ROOT_SIZE" "$IMG")"
mkdir -p "$MNT"
mount "$LOOP" "$MNT"
FREE_MB=$(( $(stat -f -c '%a*%S' "$MNT") / 1048576 ))
[ "$FREE_MB" -ge 40 ] || die "only ${FREE_MB} MiB free on the root filesystem"
PYVER=""
for d in "$MNT"/usr/lib/python3.[0-9]*; do [ -d "$d" ] && PYVER="$(basename "$d")" && break; done
[ -n "$PYVER" ] || die "no python3 in the base image"
log "root fs has ${FREE_MB} MiB free, image python is $PYVER"

log "installing figpi $VERSION into /opt/figpi"
rm -rf "$MNT/opt/figpi"
mkdir -p "$MNT/opt/figpi"
(cd "$REPO" && tar --exclude="__pycache__" --exclude="*.pyc" -cf - figpi) | tar -C "$MNT/opt/figpi" -xf -
cat > "$MNT/opt/figpi/BUILD" <<INFO
figpi $VERSION
base: $(basename "$BASE_XZ")
built: $(date -u +%Y-%m-%dT%H:%M:%SZ)
python: $PYVER
INFO
chmod -R a+rX "$MNT/opt/figpi"

log "installing overlay (systemd units, launcher, console banner)"
cp -a "$HERE/overlay/." "$MNT/"
chmod 755 "$MNT/usr/local/bin/figpi"
mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants" "$MNT/var/lib/figpi"
for unit in figpi.service figpi-firstboot.service figpi-display.service; do
  ln -sf "/etc/systemd/system/$unit" "$MNT/etc/systemd/system/multi-user.target.wants/$unit"
done

if [ -n "$HOSTNAME_IN_IMAGE" ]; then
  echo "$HOSTNAME_IN_IMAGE" > "$MNT/etc/hostname"
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME_IN_IMAGE/" "$MNT/etc/hosts"
  grep -q "^127.0.1.1" "$MNT/etc/hosts" || printf '127.0.1.1\t%s\n' "$HOSTNAME_IN_IMAGE" >> "$MNT/etc/hosts"
fi

# byte-compile with the image's python version if the host has it (purely a boot-speed nicety)
if command -v "$PYVER" >/dev/null; then
  "$PYVER" -m compileall -q "$MNT/opt/figpi/figpi" || true
fi

sync
umount "$MNT"
losetup -d "$LOOP"; LOOP=""

# --- 4. boot partition: the user-editable config, quiet boot ----------------------------
export MTOOLS_SKIP_CHECK=1
log "writing figpi.toml to the boot partition"
mcopy -i "$IMG@@$BOOT_OFF" -o "$HERE/boot/figpi.toml" ::figpi.toml
mdir -i "$IMG@@$BOOT_OFF" ::figpi.toml >/dev/null || die "figpi.toml did not land on the boot partition"

# Keep the attached screen black from power-on: no boot text, no cursor, no console blanking, no rainbow splash.
log "quieting the boot console"
mtype -i "$IMG@@$BOOT_OFF" ::cmdline.txt > "$WORK/cmdline.txt"
python3 "$HERE/quiet-boot.py" cmdline "$WORK/cmdline.txt"
mcopy -i "$IMG@@$BOOT_OFF" -o "$WORK/cmdline.txt" ::cmdline.txt
mtype -i "$IMG@@$BOOT_OFF" ::config.txt > "$WORK/config.txt"
python3 "$HERE/quiet-boot.py" config "$WORK/config.txt"
mcopy -i "$IMG@@$BOOT_OFF" -o "$WORK/config.txt" ::config.txt

# --- 5. output ------------------------------------------------------------------------------
NAME="figpi-$VERSION-$BASE_DATE-arm64-lite.img"
if [ "$COMPRESS" -eq 1 ]; then
  log "compressing to $OUT/$NAME.xz (this takes a few minutes)"
  xz -T0 -6 -c "$IMG" > "$OUT/$NAME.xz"
  (cd "$OUT" && sha256sum "$NAME.xz" > "$NAME.xz.sha256")
  log "done: $OUT/$NAME.xz ($(du -h "$OUT/$NAME.xz" | cut -f1))"
else
  mv "$IMG" "$OUT/$NAME"
  (cd "$OUT" && sha256sum "$NAME" > "$NAME.sha256")
  log "done: $OUT/$NAME"
fi

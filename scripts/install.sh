#!/usr/bin/env bash
# Install (or update) figpi on a Raspberry Pi that is already running Raspberry Pi OS,
# without reflashing.  Run from a checkout of the repository:
#   sudo scripts/install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v python3 >/dev/null || { echo "python3 is required (it ships with Raspberry Pi OS)" >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 11), "python 3.11+ required"'

echo "==> installing package to /opt/figpi"
rm -rf /opt/figpi
mkdir -p /opt/figpi
(cd "$HERE" && tar --exclude="__pycache__" --exclude="*.pyc" -cf - figpi) | tar -C /opt/figpi -xf -
echo "figpi $(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('/opt/figpi/figpi/__init__.py').read()).group(1))") (installed $(date -u +%F))" > /opt/figpi/BUILD

echo "==> installing launcher, units and console banner"
cp -a "$HERE/image/overlay/." /
chmod 755 /usr/local/bin/figpi
mkdir -p /var/lib/figpi

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
if [ ! -f "$BOOT/figpi.toml" ]; then
  echo "==> writing default config to $BOOT/figpi.toml"
  cp "$HERE/image/boot/figpi.toml" "$BOOT/figpi.toml"
fi

systemctl daemon-reload
systemctl enable figpi-firstboot.service figpi.service >/dev/null
systemctl restart figpi-firstboot.service || true
systemctl restart figpi.service
echo "==> figpi is running.  Edit $BOOT/figpi.toml, then: sudo systemctl restart figpi"
echo "    web UI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080   logs: journalctl -u figpi -f"

#!/usr/bin/env bash
# Install or update stx-helper, the small host service that runs shutdown,
# reboot, Docker restart, image cleanup, app updates and drive health checks
# for stx-monitor.
# Run it with sudo: sudo ~/stx-monitor/scripts/install-helper.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo." >&2
  exit 1
fi

cd "$(dirname "$0")/.."
# smartctl reads each drive's own health record (SMART) for the Storage page.
if ! command -v smartctl >/dev/null 2>&1; then
  echo "Installing smartmontools for drive health checks"
  apt-get install -y smartmontools >/dev/null || echo "Could not install smartmontools; drive health will be unavailable." >&2
fi
install -D -m 0755 host/stx-helper.py /usr/local/lib/stx-monitor/stx-helper.py
install -D -m 0644 host/stx-helper.service /etc/systemd/system/stx-helper.service
systemctl daemon-reload
systemctl enable stx-helper.service
systemctl restart stx-helper.service
sleep 1
systemctl --no-pager --lines=3 status stx-helper.service

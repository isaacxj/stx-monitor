#!/usr/bin/env bash
# Install or update stx-backup: nightly backups of every app's data to the
# backup drive, at 3 AM. Safe to run again.
# Run it with sudo: sudo ~/stx-monitor/scripts/install-backup.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo." >&2
  exit 1
fi
cd "$(dirname "$0")/.."

if ! command -v restic >/dev/null; then
  echo "Installing restic (the backup program)"
  apt-get install -y -q restic
fi

install -D -m 0755 host/stx-backup.py /usr/local/lib/stx-monitor/stx-backup.py
ln -sf /usr/local/lib/stx-monitor/stx-backup.py /usr/local/sbin/stx-backup
install -D -m 0644 host/stx-backup.service /etc/systemd/system/stx-backup.service
install -D -m 0644 host/stx-backup.timer /etc/systemd/system/stx-backup.timer
install -d -m 0755 /var/lib/stx-backup
install -d -m 0700 /etc/stx-backup

if [[ ! -f /etc/stx-backup/env ]]; then
  cat > /etc/stx-backup/env <<'EOF'
# stx-backup settings. Defaults shown; uncomment to change.
#STX_BACKUP_MOUNT=/mnt/backup
#STX_BACKUP_REPO=/mnt/backup/stx-restic
# Folders never backed up (colon separated). The media drive is re-downloadable.
#STX_BACKUP_SKIP_PATHS=/srv/data
# Containers never paused during a backup (comma separated).
#STX_BACKUP_NO_PAUSE=adguard
#STX_BACKUP_KEEP_DAILY=7
#STX_BACKUP_KEEP_WEEKLY=4
#STX_BACKUP_KEEP_MONTHLY=6
EOF
fi

new_password=0
if [[ ! -f /etc/stx-backup/password ]]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/stx-backup/password
  chmod 600 /etc/stx-backup/password
  new_password=1
fi

systemctl daemon-reload
systemctl enable --now stx-backup.timer

if mountpoint -q /mnt/backup; then
  stx-backup init
else
  echo
  echo "The backup drive isn't mounted at /mnt/backup yet. Mount it (see the README), then run:"
  echo "  sudo stx-backup init && sudo systemctl start stx-backup"
fi

if [[ $new_password = 1 ]]; then
  cat <<EOF

=====================================================================
 Backup password. Save it in your password manager NOW.

   $(cat /etc/stx-backup/password)

 The backups are encrypted with it. If this server's SSD dies, this
 password is the only way to read them on another machine.
=====================================================================
EOF
fi

echo
systemctl list-timers stx-backup.timer --no-pager

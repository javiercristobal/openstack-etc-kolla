#!/usr/bin/env bash
# Run on the Flask VM from the unzipped flask-order directory:  sudo bash scripts/install-site.sh
# Copies the site into /opt/flask-killbill, keeps your venv and /etc/kb-order.env,
# installs requirements and restarts kb-order. Previous code is saved in /opt/flask-killbill.bak-<time>.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
dest=/opt/flask-killbill
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
[ -d "$dest" ] || { echo "$dest not found"; exit 1; }
backup="$dest.bak-$(date +%Y%m%d-%H%M%S)"
rsync -a --exclude venv --exclude '__pycache__' "$dest"/ "$backup"/
rsync -a --exclude 'deploy/kb-order.env.example' "$here/site/" "$dest/"
# the service runs as www-data: every app file must be readable by it
find "$dest" -path "$dest/venv" -prune -o -type d -exec chmod a+rx {} + -o -type f -exec chmod a+r {} +
"$dest/venv/bin/pip" install -q -r "$dest/requirements.txt"
grep -q '^DOMAIN_CHECK=' /etc/kb-order.env || echo "NOTE: DOMAIN_CHECK is not in /etc/kb-order.env; the default (auto) is used. No token is needed."
systemctl restart kb-order
# gunicorn can take 10+ seconds to start on this lab, so wait instead of checking once
for i in $(seq 1 40); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/healthz 2>/dev/null; then
    echo "healthz: 200 after ${i}s"; echo "Backup of the old code: $backup"; exit 0
  fi
  sleep 1
done
echo "The site did not answer on 127.0.0.1:8000 within 40 seconds. Last log lines:"
journalctl -u kb-order -n 25 --no-pager | grep -v "Scheduled restart" || true
exit 1

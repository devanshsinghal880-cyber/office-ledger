#!/bin/bash
# Office Ledger installer for Ubuntu (AWS EC2). Run:  sudo bash install.sh
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="${SUDO_USER:-ubuntu}"
echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3-venv sqlite3 curl gpg debian-keyring debian-archive-keyring apt-transport-https

echo "==> Installing the app"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
sudo -u "$APP_USER" mkdir -p "$APP_DIR/data" "$APP_DIR/backups"

cat > /etc/systemd/system/ledger.service <<UNIT
[Unit]
Description=Office Ledger (Streamlit)
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=LEDGER_DB=$APP_DIR/data/ledger.db
ExecStart=$APP_DIR/venv/bin/streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false --theme.primaryColor=#b8924a
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ledger
systemctl restart ledger

echo "==> Installing Caddy (free HTTPS)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi
IP="$(curl -s https://checkip.amazonaws.com | tr -d '[:space:]')"
DOMAIN="${IP//./-}.sslip.io"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    reverse_proxy 127.0.0.1:8501
}
CADDY
systemctl restart caddy

echo "==> Daily backup at 2 AM (kept for 60 days)"
cat > /etc/cron.d/ledger-backup <<CRON
0 2 * * * $APP_USER sqlite3 $APP_DIR/data/ledger.db ".backup '$APP_DIR/backups/ledger-\$(date +\%F).db'" && find $APP_DIR/backups -name '*.db' -mtime +60 -delete
CRON

echo
echo "============================================================"
echo " Office Ledger is ready:  https://$DOMAIN"
echo " First login: Manager / manager@123   HR / hr@123"
echo " Change both passwords right away in Settings."
echo "============================================================"

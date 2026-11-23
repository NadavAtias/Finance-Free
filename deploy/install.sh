#!/usr/bin/env bash
# Finance Free: one-time setup of a fresh Ubuntu 24.04 server (Hetzner or any VPS).
#
# Run as root on the new server:
#   curl -fsSL https://raw.githubusercontent.com/NadavAtias/Finance-Free/main/deploy/install.sh -o install.sh
#   bash install.sh
#
# What it does:
#   1. Updates the system and turns on automatic security updates.
#   2. Firewall: only SSH is reachable from the internet. The dashboard is never public.
#   3. SSH: turns off password logins (only when a key is already installed).
#   4. Installs Docker, downloads Finance Free to /opt/finance-free and starts it.
#   5. Asks for your Telegram bot token and chat id (optional) and sends a test message.
# Safe to run again: it skips what is already done and keeps your .env and state.
set -euo pipefail

REPO_URL="${FF_REPO_URL:-https://github.com/NadavAtias/Finance-Free.git}"
DIR="${FF_DIR:-/opt/finance-free}"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run this as root (or with sudo)."; exit 1; }
export DEBIAN_FRONTEND=noninteractive

say "Updating the system"
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq git curl ca-certificates ufw unattended-upgrades fail2ban docker.io docker-compose-v2
dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now docker fail2ban

say "Firewall: allow SSH only"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ufw status | sed -n '1,6p'

if [ -s /root/.ssh/authorized_keys ]; then
  say "SSH: key logins only"
  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
    > /etc/ssh/sshd_config.d/10-finance-free.conf
  systemctl reload ssh || systemctl reload sshd || true
else
  say "SSH: no key found in /root/.ssh/authorized_keys, so password login stays on. Add a key and run this again."
fi

say "Downloading Finance Free to $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO_URL" "$DIR"
fi
cd "$DIR"
mkdir -p state data runs

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  if [ -t 0 ]; then
    say "Telegram alerts (press Enter to skip)"
    read -r -p "Bot token from @BotFather: " tok || true
    read -r -p "Your chat id: " chat || true
    sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=${tok:-}|; s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=${chat:-}|" .env
  fi
fi

# shellcheck disable=SC1091
set -a; . ./.env; set +a
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  if curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
       --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
       --data-urlencode "text=Finance Free: server $(hostname) is set up. You'll get a summary after each US close." >/dev/null; then
    echo "Telegram test message sent."
  else
    echo "Telegram test failed: check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in $DIR/.env"
  fi
fi

say "Building and starting (first build takes a few minutes)"
docker compose -f docker/compose.yaml up -d --build
sleep 5
docker compose -f docker/compose.yaml ps

IP="$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
cat <<EOF

Done. Finance Free is running and starts again by itself after a reboot.

Open the dashboard from your Mac (it is not reachable from the internet):
  ssh -N -L 8000:localhost:8000 root@${IP}
  then open http://localhost:8000

Useful commands on the server:
  cd $DIR
  docker compose -f docker/compose.yaml logs -f paper    # what the daily run is doing
  bash deploy/update.sh                                  # get the latest version
EOF

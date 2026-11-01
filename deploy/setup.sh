#!/usr/bin/env bash
# Установка и обновление бота на Ubuntu/Debian-сервере. Запускать от root:
#   bash setup.sh https://github.com/ВЛАДЕЛЕЦ/РЕПОЗИТОРИЙ.git
# Повторный запуск обновляет код и перезапускает бота; ключи не спрашиваются, если уже сохранены.
set -euo pipefail

REPO="${1:-}"
APP_DIR=/opt/tarot
DATA_DIR=/var/lib/tarot
ENV_FILE=/etc/tarot.env

[ "$(id -u)" = 0 ] || { echo "Запустите от root (sudo -i)."; exit 1; }
[ -n "$REPO" ] || { echo "Укажите адрес репозитория: bash setup.sh https://github.com/владелец/репозиторий.git"; exit 1; }

echo "==> Устанавливаю пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 git curl ca-certificates
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y caddy || {
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y && apt-get install -y caddy
  }
fi

echo "==> Скачиваю код"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi
id tarot >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin tarot
mkdir -p "$DATA_DIR"; chown -R tarot:tarot "$DATA_DIR"

IP="$(curl -4 -fsS https://api.ipify.org)"
HOST="$(echo "$IP" | tr . -).sslip.io"   # бесплатное имя для IP, под него Caddy сам получает HTTPS-сертификат

if [ ! -f "$ENV_FILE" ]; then
  echo "==> Настройка. Ввод скрыт, ключи сохраняются только на этом сервере."
  read -rsp "Токен бота от BotFather: " BOT_TOKEN; echo
  read -rsp "Ключ нейронки (Groq, начинается с gsk_): " LLM_KEY; echo
  umask 077
  cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$BOT_TOKEN
LLM_PROVIDER=openai
OPENAI_API_KEY=$LLM_KEY
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_MODEL=openai/gpt-oss-120b
WEBAPP_URL=https://$HOST
DATA_DIR=$DATA_DIR
PORT=8080
EOF
fi
chmod 600 "$ENV_FILE"

echo "==> Создаю службу"
cat > /etc/systemd/system/tarot.service <<EOF
[Unit]
Description=Tarot Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=tarot
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    reverse_proxy 127.0.0.1:8080
}
EOF

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp
fi

systemctl daemon-reload
systemctl enable --now tarot
systemctl restart tarot
systemctl enable caddy
systemctl restart caddy

echo "==> Проверка (HTTPS-сертификат может выпускаться до минуты)"
for i in $(seq 1 30); do
  if curl -fsS -m 5 "https://$HOST/api/spreads" >/dev/null 2>&1; then
    echo "ГОТОВО. Мини-апп: https://$HOST"
    echo "Логи бота:  journalctl -u tarot -f"
    exit 0
  fi
  sleep 3
done
echo "Бот запущен, но HTTPS пока недоступен. Проверьте, что порты 80 и 443 открыты в панели хостинга."
echo "Логи:  journalctl -u tarot -n 50     journalctl -u caddy -n 50"
exit 1

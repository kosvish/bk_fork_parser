#!/bin/bash
# Скрипт установки сканера на Ubuntu 22.04
# Запускать от root: bash setup_server.sh

set -e
echo "=== Установка арб-сканера ==="

# 1. Обновление системы
apt update && apt upgrade -y

# 2. Python 3.11
apt install -y python3.11 python3.11-venv python3-pip git curl

# 3. Пользователь для сервиса (без root прав)
useradd -r -m -d /opt/scanner -s /bin/bash scanner 2>/dev/null || true

# 4. Настройка проекта
cd /opt/scanner
python3.11 -m venv venv
source venv/bin/activate

# 5. Зависимости Python
pip install --upgrade pip
pip install -r requirements.txt

# 6. Playwright и системные зависимости Chromium
playwright install chromium
playwright install-deps chromium

# 7. Права на файлы
chown -R scanner:scanner /opt/scanner

# 8. Установка systemd сервиса
cp scanner.service /etc/systemd/system/scanner.service
systemctl daemon-reload
systemctl enable scanner
systemctl start scanner

echo ""
echo "=== Готово! ==="
echo "Статус: systemctl status scanner"
echo "Логи:   journalctl -u scanner -f"
echo "Стоп:   systemctl stop scanner"

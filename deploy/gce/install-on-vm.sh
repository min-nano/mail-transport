#!/usr/bin/env bash
# VM 上で実行されるインストーラ。20-deploy-app.sh から呼ばれる。
# 直接実行する場合は展開済みの配布物のあるディレクトリで sudo 実行すること。
set -euo pipefail

APP_DIR="/opt/mail-transport"
STATE_DIR="/var/lib/mail-transport"
ENV_FILE="/etc/mail-transport.env"
SERVICE_USER="mailtransport"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root で実行してください (sudo)。" >&2
  exit 1
fi

echo "==> 依存パッケージを導入します"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

echo "==> 実行ユーザー ${SERVICE_USER} を用意します"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> アプリを ${APP_DIR} に配置します"
install -d -m 0755 "${APP_DIR}"
rm -rf "${APP_DIR}/src" "${APP_DIR}/pyproject.toml" "${APP_DIR}/requirements-vm.txt"
cp -r "${SRC_DIR}/src" "${APP_DIR}/src"
cp "${SRC_DIR}/pyproject.toml" "${SRC_DIR}/requirements-vm.txt" "${APP_DIR}/"

echo "==> 仮想環境を用意します"
if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/venv"
fi
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements-vm.txt"
"${APP_DIR}/venv/bin/pip" install --quiet --no-deps "${APP_DIR}"

echo "==> 状態ディレクトリを用意します"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${STATE_DIR}"

echo "==> 環境設定を ${ENV_FILE} に書き出します"
install -m 0640 -o root -g "${SERVICE_USER}" "${SRC_DIR}/mail-transport.env" "${ENV_FILE}"

echo "==> systemd サービスを登録します"
install -m 0644 "${SRC_DIR}/mail-transport.service" /etc/systemd/system/mail-transport.service
systemctl daemon-reload
systemctl enable mail-transport.service >/dev/null
systemctl restart mail-transport.service

sleep 3
systemctl --no-pager --lines=20 status mail-transport.service || true

#!/usr/bin/env bash
# ソースを VM に転送し、systemd サービスとして起動する。
# 更新時も同じスクリプトを流せばよい (再起動まで行う)。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source config.env
: "${PROJECT_ID:?}" "${VM_NAME:?}" "${VM_ZONE:?}" "${ICLOUD_USERNAME:?}"

gcloud config set project "${PROJECT_ID}" >/dev/null

REPO_ROOT="$(cd .. && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

echo "==> 配布物を用意します"
mkdir -p "${STAGE}/mail-transport"
cp -r "${REPO_ROOT}/src" "${STAGE}/mail-transport/src"
cp "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/requirements-vm.txt" "${STAGE}/mail-transport/"
cp gce/mail-transport.service gce/install-on-vm.sh "${STAGE}/mail-transport/"
# ローカルのビルド成果物を持ち込まない
find "${STAGE}" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "${STAGE}" -name '*.egg-info' -type d -prune -exec rm -rf {} +

# 秘密情報そのものは書かない。Secret Manager のシークレット名だけを渡し、
# 値は VM のサービスアカウントで実行時に取得する。
cat > "${STAGE}/mail-transport/mail-transport.env" <<ENVEOF
ICLOUD_USERNAME=${ICLOUD_USERNAME}
ICLOUD_APP_PASSWORD_SECRET=icloud-app-password
GMAIL_OAUTH_JSON_SECRET=gmail-oauth
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
STATE_BACKEND=sqlite
STATE_DB_PATH=/var/lib/mail-transport/state.db
INITIAL_IMPORT=${INITIAL_IMPORT:-none}
IDLE_ENABLED=true
SAFETY_SYNC_SECONDS=${SAFETY_SYNC_SECONDS:-300}
MAX_MESSAGES_PER_RUN=${MAX_MESSAGES_PER_RUN:-40}
LOG_LEVEL=${LOG_LEVEL:-INFO}
ENVEOF

tar -czf "${STAGE}/mail-transport.tar.gz" -C "${STAGE}" mail-transport

echo "==> VM に転送します"
gcloud compute scp "${STAGE}/mail-transport.tar.gz" \
  "${VM_NAME}:/tmp/mail-transport.tar.gz" --zone="${VM_ZONE}"

echo "==> VM 上でインストールします"
gcloud compute ssh "${VM_NAME}" --zone="${VM_ZONE}" --command="
  set -euo pipefail
  rm -rf /tmp/mail-transport
  tar -xzf /tmp/mail-transport.tar.gz -C /tmp
  chmod +x /tmp/mail-transport/install-on-vm.sh
  sudo /tmp/mail-transport/install-on-vm.sh
  rm -rf /tmp/mail-transport /tmp/mail-transport.tar.gz
"

echo
echo "完了しました。ログを見るには:"
echo "  gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --command='sudo journalctl -u mail-transport -f'"

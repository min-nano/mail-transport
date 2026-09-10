#!/usr/bin/env bash
# ソースを VM に転送し、systemd サービスとして起動する。
# 更新時も同じスクリプトを流せばよい (再起動まで行う)。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source lib.sh
load_deploy_config .
require_vars PROJECT_ID VM_NAME VM_ZONE ICLOUD_USERNAME

gcloud config set project "${PROJECT_ID}" >/dev/null

REPO_ROOT="$(cd .. && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

echo "==> 配布物を用意します"
mkdir -p "${STAGE}/mail-transport"
cp -r "${REPO_ROOT}/src" "${STAGE}/mail-transport/src"
cp "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/requirements.txt" "${STAGE}/mail-transport/"
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
TRASH_AFTER_FORWARD=${TRASH_AFTER_FORWARD:-true}
IDLE_ENABLED=true
SAFETY_SYNC_SECONDS=${SAFETY_SYNC_SECONDS:-300}
MAX_MESSAGES_PER_RUN=${MAX_MESSAGES_PER_RUN:-40}
LOG_LEVEL=${LOG_LEVEL:-INFO}
ENVEOF

# VM を作り直したときに同期位置を引き継ぐ。VM 側に state.db が無いときだけ
# 復元されるので、稼働中の VM に流しても影響はない。
if [[ -n "${STATE_DB_FILE:-}" ]]; then
  if [[ ! -f "${STATE_DB_FILE}" ]]; then
    echo "STATE_DB_FILE が見つかりません: ${STATE_DB_FILE}" >&2
    exit 1
  fi
  cp "${STATE_DB_FILE}" "${STAGE}/mail-transport/state.db"
  echo "    同期位置 ${STATE_DB_FILE} を同梱します"
fi

tar -czf "${STAGE}/mail-transport.tar.gz" -C "${STAGE}" mail-transport

# CI から実行するときは IAP トンネル越しにする (公開 SSH を開けずに済む)
mapfile -t SSH_ARGS < <(gcloud_ssh_args)

echo "==> VM に転送します"
gcloud compute scp --quiet "${SSH_ARGS[@]}" \
  "${STAGE}/mail-transport.tar.gz" "${VM_NAME}:/tmp/mail-transport.tar.gz"

echo "==> VM 上でインストールします"
# 更新は「止めて入れ替えて起動」の単純な手順。数秒の停止は許容する。
gcloud compute ssh --quiet "${SSH_ARGS[@]}" "${VM_NAME}" --command="
  set -euo pipefail
  rm -rf /tmp/mail-transport
  tar -xzf /tmp/mail-transport.tar.gz -C /tmp
  chmod +x /tmp/mail-transport/install-on-vm.sh
  sudo /tmp/mail-transport/install-on-vm.sh
  rm -rf /tmp/mail-transport /tmp/mail-transport.tar.gz
"

echo "==> 稼働を確認します"
# 起動直後に落ちる設定ミスを検出する。active でなければ非ゼロで終わる。
gcloud compute ssh --quiet "${SSH_ARGS[@]}" "${VM_NAME}" --command="
  set -euo pipefail
  for i in \$(seq 1 15); do
    if systemctl is-active --quiet mail-transport; then
      echo 'mail-transport は稼働中です'
      sudo journalctl -u mail-transport --no-pager --lines=30
      exit 0
    fi
    sleep 2
  done
  echo '起動に失敗しました' >&2
  sudo journalctl -u mail-transport --no-pager --lines=60 >&2
  exit 1
"

echo
echo "完了しました。ログを見るには:"
echo "  gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --command='sudo journalctl -u mail-transport -f'"

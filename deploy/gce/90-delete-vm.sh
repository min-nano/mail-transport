#!/usr/bin/env bash
# VM を削除する (作り直しの前段)。
#
# 削除するのは VM だけ。サービスアカウント・シークレット・Workload Identity・
# ファイアウォール規則は残るので、provision.sh を流せばそのまま戻せる。
#
# 削除前に同期位置 (state.db) を手元に退避する。これが無いと、作り直した VM は
# 「起動時点より後のメール」しか転送しなくなり、停止中に届いたぶんを取りこぼす。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source lib.sh
load_deploy_config .
require_vars PROJECT_ID VM_NAME VM_ZONE

BACKUP_DIR="${BACKUP_DIR:-state-backup}"
gcloud config set project "${PROJECT_ID}" >/dev/null

if ! gcloud compute instances describe "${VM_NAME}" --zone="${VM_ZONE}" >/dev/null 2>&1; then
  echo "VM ${VM_NAME} は存在しません。何もしません。"
  exit 0
fi

if [[ "${ASSUME_YES:-false}" != "true" ]]; then
  echo "VM ${VM_NAME} (${VM_ZONE}) を削除します。取り消せません。"
  read -r -p "続けるならVM名を入力してください: " answer
  if [[ "${answer}" != "${VM_NAME}" ]]; then
    echo "入力が一致しないため中止しました。" >&2
    exit 1
  fi
fi

mapfile -t SSH_ARGS < <(gcloud_ssh_args)

echo "==> 同期位置を退避します"
mkdir -p "${BACKUP_DIR}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="${BACKUP_DIR}/${VM_NAME}-${STAMP}.db"
# サービスを止めてから複製する。動いたまま複製すると WAL の途中を掴みうる。
if gcloud compute ssh --quiet "${SSH_ARGS[@]}" "${VM_NAME}" --command="
  set -euo pipefail
  sudo systemctl stop mail-transport 2>/dev/null || true
  if [[ -f /var/lib/mail-transport/state.db ]]; then
    sudo install -m 0644 -o \"\$(id -un)\" /var/lib/mail-transport/state.db /tmp/state.db
  else
    echo '状態ファイルがありません' >&2
    exit 1
  fi
"; then
  gcloud compute scp --quiet "${SSH_ARGS[@]}" "${VM_NAME}:/tmp/state.db" "${BACKUP_FILE}"
  cp "${BACKUP_FILE}" "${BACKUP_DIR}/state.db"
  echo "    deploy/${BACKUP_FILE} に退避しました (deploy/${BACKUP_DIR}/state.db としても複製)"
else
  echo "    退避できませんでした。作り直した VM は起動時点以降のメールだけを転送します。" >&2
  if [[ "${ASSUME_YES:-false}" != "true" ]]; then
    read -r -p "それでも削除しますか? [y/N] " answer
    [[ "${answer}" == "y" || "${answer}" == "Y" ]] || exit 1
  fi
fi

echo "==> VM を削除します"
gcloud compute instances delete "${VM_NAME}" --zone="${VM_ZONE}" --quiet

cat <<SUMMARY

削除しました。作り直すには:

  ./deploy/provision.sh

同期位置を引き継ぐなら:

  ./deploy/provision.sh --state-db deploy/${BACKUP_DIR}/state.db

サービスアカウント・シークレット・Workload Identity は残しています。
VM に紐づいていた IAM (osAdminLogin / IAP) は VM と一緒に消えるため、
provision.sh が作り直しの中で付け直します。
SUMMARY

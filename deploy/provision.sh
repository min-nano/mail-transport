#!/usr/bin/env bash
# GCP 側を丸ごと用意して、常駐サービスが動くところまで持っていく。
#
#   ./deploy/provision.sh
#
# 何度実行しても同じ結果になる (冪等)。VM を作り直したあとに流し直せば、
# 消えたもの (VM、VM に紐づく IAM、アプリ) だけが作り直される。
#
# 実行される順序と、その順序である理由:
#   1. gce/00-setup.sh    API・サービスアカウント・シークレット
#   2. gce/10-create-vm.sh VM 本体 (2 の前に SA が要る)
#   3. ci/00-setup-wif.sh  Workload Identity と「VM 単位」の IAM
#                          → VM を作り直すとこの IAM は消えるので、
#                            VM 作成の「後」に必ず流す必要がある
#   4. gce/20-deploy-app.sh アプリの配置と systemd 起動
set -euo pipefail

# 相対パスの指定は、この cd より前の場所を基準に解決する
ORIGINAL_PWD="$PWD"
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source lib.sh

usage() {
  cat <<'USAGE'
使い方: ./deploy/provision.sh [オプション]

  --skip-ci            GitHub Actions 用の Workload Identity 設定を飛ばす
                       (CI からデプロイしない場合。VM 単位の IAM も付きません)
  --rotate-secrets     登録済みのシークレットを入れ替える (再入力を求められます)
  --state-db <ファイル> 同期位置を引き継ぐ (90-delete-vm.sh が退避したもの)
  --yes                確認を求めない (無料枠から外れる設定でも続行します)
  -h, --help           この説明を表示

例:
  ./deploy/provision.sh                                         # 新規構築 / 再実行
  ./deploy/provision.sh --state-db deploy/state-backup/state.db # 作り直しで位置を引き継ぐ
  ./deploy/provision.sh --skip-ci                               # 手動デプロイだけで使う
USAGE
}

SKIP_CI=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ci)        SKIP_CI=true; shift ;;
    --rotate-secrets) export ROTATE_SECRETS=true; shift ;;
    --yes|-y)         export ASSUME_YES=true; shift ;;
    --state-db)
      [[ $# -ge 2 ]] || { echo "--state-db にはファイルを指定してください" >&2; exit 1; }
      # サブスクリプトは deploy/ で動くので、ここで絶対パスに直しておく
      case "$2" in
        /*) STATE_DB_FILE="$2" ;;
        *)  STATE_DB_FILE="${ORIGINAL_PWD}/$2" ;;
      esac
      export STATE_DB_FILE
      shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; usage >&2; exit 1 ;;
  esac
done

load_deploy_config .
require_vars PROJECT_ID VM_NAME VM_ZONE RUNTIME_SA_NAME ICLOUD_USERNAME

# 途中まで進んでから足りないものに気付くと後始末が面倒なので、先に確かめる
command -v gcloud >/dev/null || { echo "gcloud が見つかりません。" >&2; exit 1; }
if ! gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q .; then
  echo "gcloud にログインしていません。gcloud auth login を実行してください。" >&2
  exit 1
fi
if [[ -n "${STATE_DB_FILE:-}" && ! -f "${STATE_DB_FILE}" ]]; then
  echo "--state-db のファイルが見つかりません: ${STATE_DB_FILE}" >&2
  exit 1
fi

run_step() {
  local number="$1" title="$2"; shift 2
  echo
  echo "############################################################"
  echo "# ${number} ${title}"
  echo "############################################################"
  "$@"
}

STARTED_AT="$(date +%s)"

run_step "[1/4]" "API・サービスアカウント・シークレット" ./gce/00-setup.sh
run_step "[2/4]" "VM の作成" ./gce/10-create-vm.sh
if [[ "${SKIP_CI}" == "true" ]]; then
  echo
  echo "[3/4] Workload Identity の設定は --skip-ci のため飛ばします"
  echo "      (CI からデプロイする場合は ./deploy/ci/00-setup-wif.sh を実行してください)"
else
  run_step "[3/4]" "Workload Identity と VM 単位の IAM" ./ci/00-setup-wif.sh
fi
run_step "[4/4]" "アプリの配置と起動" ./gce/20-deploy-app.sh

cat <<SUMMARY

############################################################
# 完了しました ($(( $(date +%s) - STARTED_AT )) 秒)
############################################################

  プロジェクト: ${PROJECT_ID}
  VM          : ${VM_NAME} (${VM_ZONE})
  転送元      : ${ICLOUD_USERNAME}

ログを追う:
  gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} \\
    --command='sudo journalctl -u mail-transport -f'

作り直す:
  ./deploy/gce/90-delete-vm.sh          # 同期位置を退避してから VM を削除
  ./deploy/provision.sh --state-db deploy/state-backup/state.db
SUMMARY

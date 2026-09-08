#!/usr/bin/env bash
# GCP 側の初期構築 (GCE 構成): API 有効化 / サービスアカウント / シークレット。
# 何度実行しても安全 (冪等)。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source lib.sh
load_deploy_config .
require_vars PROJECT_ID RUNTIME_SA_NAME
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> プロジェクトを設定します: ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> 必要な API を有効化します"
# GCE 構成では Firestore も Cloud Run も Scheduler も使わない
gcloud services enable \
  compute.googleapis.com \
  secretmanager.googleapis.com \
  gmail.googleapis.com

echo "==> サービスアカウントを作成します"
if ! gcloud iam service-accounts describe "${RUNTIME_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SA_NAME}" \
    --display-name="mail-transport runtime"
else
  echo "    既に存在します"
fi

put_secret() {
  local name="$1" file="$2"
  if ! gcloud secrets describe "${name}" >/dev/null 2>&1; then
    gcloud secrets create "${name}" --replication-policy=automatic
  fi
  gcloud secrets versions add "${name}" --data-file="${file}" >/dev/null
  # この 1 つのシークレットの読み取りだけを許可する
  gcloud secrets add-iam-policy-binding "${name}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
  echo "    シークレット ${name} を更新しました"
}

echo "==> シークレットを登録します"
if [[ -n "${ICLOUD_APP_PASSWORD_FILE:-}" ]]; then
  put_secret "icloud-app-password" "${ICLOUD_APP_PASSWORD_FILE}"
else
  read -r -s -p "iCloud のアプリ用パスワード (xxxx-xxxx-xxxx-xxxx): " icloud_pw
  echo
  tmp="$(mktemp)"; trap 'rm -f "${tmp}"' EXIT
  printf '%s' "${icloud_pw}" > "${tmp}"
  put_secret "icloud-app-password" "${tmp}"
  rm -f "${tmp}"; trap - EXIT
fi

GMAIL_OAUTH_FILE="${GMAIL_OAUTH_FILE:-gmail_oauth.json}"
if [[ -f "${GMAIL_OAUTH_FILE}" ]]; then
  put_secret "gmail-oauth" "${GMAIL_OAUTH_FILE}"
else
  echo "    ${GMAIL_OAUTH_FILE} が見つかりません。"
  echo "    tools/get_gmail_refresh_token.py で作成してから再実行してください。"
  exit 1
fi

echo
echo "完了しました。次は deploy/gce/10-create-vm.sh を実行してください。"
echo "  実行用サービスアカウント: ${RUNTIME_SA}"

#!/usr/bin/env bash
# GCP 側の初期構築: API 有効化 / Firestore / シークレット / サービスアカウント。
# 何度実行しても安全 (冪等) になるようにしている。
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -f config.env ]]; then
  # shellcheck disable=SC1091
  source config.env
else
  echo "deploy/config.env がありません。deploy/config.env.example をコピーしてください。" >&2
  exit 1
fi

: "${PROJECT_ID:?}" "${REGION:?}" "${RUNTIME_SA_NAME:?}" "${INVOKER_SA_NAME:?}"

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER_SA="${INVOKER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> プロジェクトを設定します: ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> 必要な API を有効化します"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  gmail.googleapis.com

echo "==> Firestore (Native モード) を用意します"
if ! gcloud firestore databases describe --database="(default)" >/dev/null 2>&1; then
  gcloud firestore databases create --location="${REGION}" --type=firestore-native
else
  echo "    既に存在します"
fi

create_sa() {
  local name="$1" display="$2"
  if ! gcloud iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${name}" --display-name="${display}"
  else
    echo "    サービスアカウント ${name} は既に存在します"
  fi
}

echo "==> サービスアカウントを作成します"
create_sa "${RUNTIME_SA_NAME}" "mail-transport Cloud Run runtime"
create_sa "${INVOKER_SA_NAME}" "mail-transport Cloud Scheduler invoker"

echo "==> Firestore へのアクセス権を付与します"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/datastore.user" \
  --condition=None >/dev/null

put_secret() {
  local name="$1" file="$2"
  if ! gcloud secrets describe "${name}" >/dev/null 2>&1; then
    gcloud secrets create "${name}" --replication-policy=automatic
  fi
  gcloud secrets versions add "${name}" --data-file="${file}" >/dev/null
  # 実行用サービスアカウントにだけ読み取りを許可する
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
echo "完了しました。次は deploy/10-deploy.sh を実行してください。"
echo "  Cloud Run 実行 SA : ${RUNTIME_SA}"
echo "  Scheduler 呼出 SA : ${INVOKER_SA}"

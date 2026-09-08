#!/usr/bin/env bash
# Cloud Run へのデプロイと Cloud Scheduler ジョブの作成。
set -euo pipefail

cd "$(dirname "$0")"
if [[ -f config.env ]]; then
  # shellcheck disable=SC1091
  source config.env
else
  echo "deploy/config.env がありません。" >&2
  exit 1
fi

: "${PROJECT_ID:?}" "${REGION:?}" "${SERVICE_NAME:?}" "${JOB_NAME:?}" "${ICLOUD_USERNAME:?}"

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER_SA="${INVOKER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Cloud Run にデプロイします"
gcloud run deploy "${SERVICE_NAME}" \
  --source=.. \
  --region="${REGION}" \
  --service-account="${RUNTIME_SA}" \
  --no-allow-unauthenticated \
  --cpu="${CPU:-1}" \
  --memory="${MEMORY:-512Mi}" \
  --concurrency="${CONCURRENCY:-1}" \
  --min-instances="${MIN_INSTANCES:-0}" \
  --max-instances="${MAX_INSTANCES:-1}" \
  --timeout="${REQUEST_TIMEOUT:-300}" \
  --set-env-vars="ICLOUD_USERNAME=${ICLOUD_USERNAME},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},MAX_MESSAGES_PER_RUN=${MAX_MESSAGES_PER_RUN:-40},RUN_BUDGET_SECONDS=${RUN_BUDGET_SECONDS:-240},INITIAL_IMPORT=${INITIAL_IMPORT:-none}" \
  --set-secrets="ICLOUD_APP_PASSWORD=icloud-app-password:latest,GMAIL_OAUTH_JSON=gmail-oauth:latest"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format='value(status.url)')"
echo "==> サービス URL: ${SERVICE_URL}"

echo "==> Scheduler の呼び出し権限を付与します"
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:${INVOKER_SA}" \
  --role="roles/run.invoker" >/dev/null

echo "==> Cloud Scheduler ジョブを作成/更新します (${SCHEDULE:-* * * * *})"
SCHED_ARGS=(
  --location="${REGION}"
  --schedule="${SCHEDULE:-* * * * *}"
  --time-zone="${SCHEDULE_TZ:-Etc/UTC}"
  --uri="${SERVICE_URL}/sync"
  --http-method=POST
  --oidc-service-account-email="${INVOKER_SA}"
  --oidc-token-audience="${SERVICE_URL}"
  --attempt-deadline=320s
  --max-retry-attempts=1
)
if gcloud scheduler jobs describe "${JOB_NAME}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${JOB_NAME}" "${SCHED_ARGS[@]}"
else
  gcloud scheduler jobs create http "${JOB_NAME}" "${SCHED_ARGS[@]}"
fi

echo
echo "完了しました。手動で 1 回動かすには:"
echo "  gcloud scheduler jobs run ${JOB_NAME} --location=${REGION}"
echo "ログを見るには:"
echo "  gcloud run services logs read ${SERVICE_NAME} --region=${REGION} --limit=50"

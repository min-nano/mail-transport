#!/usr/bin/env bash
# Always Free 対象の e2-micro を作成する。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source config.env
: "${PROJECT_ID:?}" "${VM_NAME:?}" "${VM_ZONE:?}" "${RUNTIME_SA_NAME:?}"

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud config set project "${PROJECT_ID}" >/dev/null

MACHINE_TYPE="${VM_MACHINE_TYPE:-e2-micro}"
DISK_TYPE="${VM_DISK_TYPE:-pd-standard}"
DISK_SIZE="${VM_DISK_SIZE_GB:-30}"
REGION="${VM_ZONE%-*}"

# 無料枠から外れる設定は事前に止める
case "${REGION}" in
  us-west1|us-central1|us-east1) ;;
  *)
    echo "警告: ${REGION} は Always Free の対象外です (us-west1 / us-central1 / us-east1 のみ)。" >&2
    read -r -p "課金される可能性がありますが続けますか? [y/N] " answer
    [[ "${answer}" == "y" || "${answer}" == "Y" ]] || exit 1
    ;;
esac
if [[ "${MACHINE_TYPE}" != "e2-micro" ]]; then
  echo "警告: ${MACHINE_TYPE} は Always Free の対象外です (e2-micro のみ)。" >&2
fi
if [[ "${DISK_TYPE}" != "pd-standard" ]]; then
  echo "警告: ${DISK_TYPE} は Always Free の対象外です (pd-standard のみ)。" >&2
fi
if (( DISK_SIZE > 30 )); then
  echo "警告: ディスク ${DISK_SIZE}GB は無料枠 (30GB) を超えます。" >&2
fi

if gcloud compute instances describe "${VM_NAME}" --zone="${VM_ZONE}" >/dev/null 2>&1; then
  echo "VM ${VM_NAME} は既に存在します。"
else
  echo "==> VM を作成します (${MACHINE_TYPE} / ${VM_ZONE} / ${DISK_TYPE} ${DISK_SIZE}GB)"
  gcloud compute instances create "${VM_NAME}" \
    --zone="${VM_ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --image-family="${VM_IMAGE_FAMILY:-debian-12}" \
    --image-project="${VM_IMAGE_PROJECT:-debian-cloud}" \
    --boot-disk-type="${DISK_TYPE}" \
    --boot-disk-size="${DISK_SIZE}GB" \
    --service-account="${RUNTIME_SA}" \
    --scopes="https://www.googleapis.com/auth/cloud-platform" \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --metadata="enable-oslogin=TRUE" \
    --labels="app=mail-transport" \
    --tags="mail-transport"
fi

echo
echo "完了しました。次は deploy/gce/20-deploy-app.sh を実行してください。"

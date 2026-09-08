#!/usr/bin/env bash
# GitHub Actions から鍵なしでデプロイできるようにする一度きりの設定。
#
# サービスアカウントキー (JSON) を GitHub に置く代わりに Workload Identity 連携を
# 使う。GitHub が発行する OIDC トークンを GCP が検証するので、盗まれて困る長期の
# 秘密情報が存在しない。信頼するのはこのリポジトリからの実行だけに限定する。
#
# 何度実行しても安全 (冪等)。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source lib.sh
load_deploy_config .
require_vars PROJECT_ID VM_NAME VM_ZONE RUNTIME_SA_NAME

GITHUB_REPO="${GITHUB_REPO:-min-nano/mail-transport}"
POOL="${WIF_POOL:-github}"
PROVIDER="${WIF_PROVIDER:-github}"
DEPLOYER_SA_NAME="${DEPLOYER_SA_NAME:-mail-transport-deployer}"
DEPLOYER_SA="${DEPLOYER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
VM_TAG="${VM_TAG:-mail-transport}"

if [[ ! "${GITHUB_REPO}" =~ ^[^/]+/[^/]+$ ]]; then
  echo "GITHUB_REPO は owner/repo 形式で指定してください: ${GITHUB_REPO}" >&2
  exit 1
fi

gcloud config set project "${PROJECT_ID}" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

echo "==> 必要な API を有効化します"
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  iap.googleapis.com \
  oslogin.googleapis.com

echo "==> Workload Identity プールを用意します"
if ! gcloud iam workload-identity-pools describe "${POOL}" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${POOL}" \
    --location=global --display-name="GitHub Actions"
else
  echo "    既に存在します"
fi

echo "==> OIDC プロバイダを用意します"
# 誰がトークンを受け取れるかを、GitHub が署名した主張 (assertion) で絞る。
#
# 1. repository       — このリポジトリ以外からの利用を拒否する。省くと同じ
#                       発行者 (= 全 GitHub リポジトリ) が対象になってしまう。
# 2. ref              — main 以外を拒否する。同一リポジトリのブランチから出した
#                       プルリクエストは権限が制限されないため、ワークフローを
#                       1 つ足すだけで id-token を取得できてしまう。fork からの
#                       PR は読み取り専用なので元々届かないが、内部ブランチは
#                       ここで止める必要がある。workflow_dispatch も任意の
#                       ブランチを選べるので同様。
#                       PR のときの ref は refs/pull/<番号>/merge になる。
# 3. job_workflow_ref — 認証できるワークフローを deploy.yml 1 本に限定する。
#                       main に別のワークフローが増えても、そこからは
#                       デプロイ用サービスアカウントに成り代われない。
#
# deploy.yml の名前を変えたときはこの条件も直すこと (直さないと認証が通らない)。
DEPLOY_WORKFLOW="${DEPLOY_WORKFLOW:-.github/workflows/deploy.yml}"
DEPLOY_WORKFLOW_REF="${GITHUB_REPO}/${DEPLOY_WORKFLOW}@refs/heads/${DEPLOY_BRANCH:-main}"
ATTRIBUTE_CONDITION="assertion.repository == '${GITHUB_REPO}'"
ATTRIBUTE_CONDITION+=" && assertion.ref == 'refs/heads/${DEPLOY_BRANCH:-main}'"
ATTRIBUTE_CONDITION+=" && assertion.job_workflow_ref == '${DEPLOY_WORKFLOW_REF}'"

PROVIDER_ARGS=(
  --location=global
  --workload-identity-pool="${POOL}"
  --display-name="GitHub Actions OIDC"
  --issuer-uri="https://token.actions.githubusercontent.com"
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.job_workflow_ref"
  --attribute-condition="${ATTRIBUTE_CONDITION}"
)
echo "    許可する条件: ${ATTRIBUTE_CONDITION}"
if gcloud iam workload-identity-pools providers describe "${PROVIDER}" \
     --location=global --workload-identity-pool="${POOL}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER}" "${PROVIDER_ARGS[@]}"
else
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" "${PROVIDER_ARGS[@]}"
fi

echo "==> デプロイ用サービスアカウントを用意します"
if ! gcloud iam service-accounts describe "${DEPLOYER_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${DEPLOYER_SA_NAME}" \
    --display-name="mail-transport CI deployer"
else
  echo "    既に存在します"
fi

echo "==> GitHub からの成り代わりを許可します (${GITHUB_REPO} のみ)"
POOL_PATH="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}"
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER_SA}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL_PATH}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

echo "==> デプロイに必要な最小権限を付与します"
# インスタンスを describe するためのプロジェクト全体の読み取り (変更権限はなし)
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/compute.viewer" \
  --condition=None >/dev/null

# SSH と sudo、および IAP トンネルは「この VM に対してだけ」許可する
for role in roles/compute.osAdminLogin roles/iap.tunnelResourceAccessor; do
  gcloud compute instances add-iam-policy-binding "${VM_NAME}" \
    --zone="${VM_ZONE}" \
    --member="serviceAccount:${DEPLOYER_SA}" \
    --role="${role}" >/dev/null
  echo "    ${role} (インスタンス ${VM_NAME} 限定)"
done

# SA がアタッチされた VM に SSH するには、その SA として振る舞う権限が要る
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/iam.serviceAccountUser" >/dev/null
echo "    roles/iam.serviceAccountUser (${RUNTIME_SA} 限定)"

echo "==> IAP から SSH するためのファイアウォール規則を用意します"
# 22 番を公開せずに済ませる。許可するのは IAP の固定レンジのみ。
NETWORK="$(gcloud compute instances describe "${VM_NAME}" --zone="${VM_ZONE}" \
  --format='value(networkInterfaces[0].network)')"
NETWORK="${NETWORK##*/}"
if gcloud compute firewall-rules describe allow-iap-ssh-mail-transport >/dev/null 2>&1; then
  echo "    既に存在します"
else
  gcloud compute firewall-rules create allow-iap-ssh-mail-transport \
    --network="${NETWORK}" \
    --direction=INGRESS \
    --action=allow \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --target-tags="${VM_TAG}" \
    --description="IAP 経由の SSH (CI デプロイ用)"
fi

cat <<SUMMARY

============================================================
完了しました。GitHub 側に次の設定を入れてください。

  Settings → Secrets and variables → Actions → Variables
    GCP_WORKLOAD_IDENTITY_PROVIDER = ${POOL_PATH}/providers/${PROVIDER}
    GCP_DEPLOYER_SA                = ${DEPLOYER_SA}
    GCP_PROJECT_ID                 = ${PROJECT_ID}
    GCP_VM_NAME                    = ${VM_NAME}
    GCP_VM_ZONE                    = ${VM_ZONE}

  Settings → Secrets and variables → Actions → Secrets
    ICLOUD_USERNAME                = ${ICLOUD_USERNAME:-you@icloud.com}

以降 ${DEPLOY_BRANCH:-main} への push で ${DEPLOY_WORKFLOW} が動きます。

このプロバイダが発行を許すのは次の条件をすべて満たす場合だけです。
  - リポジトリが ${GITHUB_REPO}
  - ref が refs/heads/${DEPLOY_BRANCH:-main}
  - ワークフローが ${DEPLOY_WORKFLOW}
プルリクエストや他ブランチからは、ワークフローを足しても認証できません。

あわせて GitHub 側でも二重に塞ぐことを勧めます:
  Settings → Environments → production → Deployment branches and tags
  → Selected branches → main を追加
============================================================
SUMMARY

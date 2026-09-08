#!/usr/bin/env bash
# 重複排除レコードを自動削除するための TTL ポリシーを設定する (任意)。
# 設定しなくても動作するが、Firestore の使用量を一定に保てる。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source config.env
: "${PROJECT_ID:?}"

gcloud firestore fields ttls update expire_at \
  --collection-group=mail_transport_seen \
  --enable-ttl \
  --project="${PROJECT_ID}"

echo "mail_transport_seen.expire_at に TTL を設定しました。"

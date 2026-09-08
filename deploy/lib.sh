#!/usr/bin/env bash
# デプロイスクリプト共通の処理。各スクリプトから source して使う。

# 設定を読み込む。
#   - 手元では deploy/config.env から
#   - CI では GitHub の Variables / Secrets が環境変数として渡ってくるので、
#     config.env が無くてもそのまま使う
load_deploy_config() {
  local dir="${1:-.}"
  if [[ -f "${dir}/config.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "${dir}/config.env"
    set +a
    return 0
  fi
  if [[ -n "${PROJECT_ID:-}" ]]; then
    echo "config.env が無いため環境変数の設定を使います。"
    return 0
  fi
  echo "deploy/config.env がありません。deploy/config.env.example をコピーしてください。" >&2
  echo "(CI から実行する場合は PROJECT_ID などを環境変数で渡してください)" >&2
  return 1
}

# 必須の設定が揃っているか確認する。足りなければ全部まとめて報告する。
require_vars() {
  local missing=() name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || missing+=("${name}")
  done
  if (( ${#missing[@]} > 0 )); then
    echo "必要な設定がありません: ${missing[*]}" >&2
    return 1
  fi
}

# SSH / SCP の共通引数。CI では IAP トンネル経由にして公開 SSH を開けずに済ませる。
gcloud_ssh_args() {
  local args=(--zone="${VM_ZONE}")
  if [[ "${USE_IAP:-false}" == "true" ]]; then
    args+=(--tunnel-through-iap)
  fi
  printf '%s\n' "${args[@]}"
}

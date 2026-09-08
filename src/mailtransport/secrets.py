"""Secret Manager からの値取得 (実行時解決を使う場合のみ)."""

from __future__ import annotations

import functools
import logging

log = logging.getLogger(__name__)


@functools.lru_cache(maxsize=16)
def access_secret(resource: str) -> str:
    """``projects/<id>/secrets/<name>/versions/latest`` 形式の値を取得する.

    ``<name>`` だけを渡した場合は GOOGLE_CLOUD_PROJECT を補完する。
    """
    import os

    from google.cloud import secretmanager

    name = resource
    if not name.startswith("projects/"):
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        if not project:
            raise ValueError(
                f"シークレット {resource!r} を解決できません: "
                "完全なリソース名か GOOGLE_CLOUD_PROJECT が必要です"
            )
        name = f"projects/{project}/secrets/{resource}"
    if "/versions/" not in name:
        name = f"{name}/versions/latest"

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")

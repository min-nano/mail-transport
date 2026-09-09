"""レビュー対象の差分を集める."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

# 判定に渡すファイル一覧の上限。数千ファイルの変更でも判断材料は十分足りる。
MAX_LISTED_FILES = 200


@dataclasses.dataclass(frozen=True)
class ChangedFile:
    path: str
    added: int
    deleted: int
    binary: bool = False

    @property
    def churn(self) -> int:
        return self.added + self.deleted


@dataclasses.dataclass(frozen=True)
class PullRequestContext:
    title: str
    body: str
    base_sha: str
    head_sha: str
    files: tuple[ChangedFile, ...]
    diff: str

    @property
    def total_added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def total_deleted(self) -> int:
        return sum(f.deleted for f in self.files)

    @property
    def churn(self) -> int:
        return self.total_added + self.total_deleted

    def summary(self) -> str:
        """判定用の要約. 差分の中身ではなく規模と対象を渡す.

        本文をまるごと渡すと判定自体が高くつくうえ、判定に必要なのは
        「どこをどれだけ触ったか」なのでファイル単位の増減で足りる。
        """
        lines = [
            f"タイトル: {self.title or '(なし)'}",
            f"変更ファイル数: {len(self.files)}",
            f"追加行数: {self.total_added} / 削除行数: {self.total_deleted}",
            "",
            "変更されたファイル (パス, +追加, -削除):",
        ]
        for changed in self.files[:MAX_LISTED_FILES]:
            marker = " [バイナリ]" if changed.binary else ""
            lines.append(f"  {changed.path}, +{changed.added}, -{changed.deleted}{marker}")
        if len(self.files) > MAX_LISTED_FILES:
            lines.append(f"  ... ほか {len(self.files) - MAX_LISTED_FILES} ファイル")
        if self.body.strip():
            lines += ["", "説明文:", self.body.strip()[:4000]]
        return "\n".join(lines)


def parse_numstat(output: str) -> tuple[ChangedFile, ...]:
    """``git diff --numstat`` の出力を解析する.

    バイナリファイルは増減が "-" になるので 0 として扱う。
    """
    files: list[ChangedFile] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], "\t".join(parts[2:])
        binary = added == "-" or deleted == "-"
        files.append(
            ChangedFile(
                path=path,
                added=0 if binary else int(added),
                deleted=0 if binary else int(deleted),
                binary=binary,
            )
        )
    return tuple(files)


def _git(args: list[str], cwd: Path | str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def collect(
    base_sha: str,
    head_sha: str,
    title: str = "",
    body: str = "",
    cwd: Path | str | None = None,
    runner=_git,
) -> PullRequestContext:
    """base と head の差分を集める.

    ``base...head`` (3 点) を使うので、base 側の後続コミットは差分に混ざらない。
    """
    rng = f"{base_sha}...{head_sha}"
    files = parse_numstat(runner(["diff", "--numstat", rng], cwd))
    diff = runner(["diff", rng], cwd)
    return PullRequestContext(
        title=title,
        body=body,
        base_sha=base_sha,
        head_sha=head_sha,
        files=files,
        diff=diff,
    )

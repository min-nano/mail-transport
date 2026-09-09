# mail-transport — iCloud → Gmail メール転送

iCloud のメールを **SMTP を経由せず** Gmail に複製します。
iCloud からは **IMAP** で生のメッセージを取り出し、Gmail へは **Gmail API の
`users.messages.insert`** で直接書き込みます。

**GCE の e2-micro を常時起動し、IMAP IDLE で新着を待ち受けます。**
Always Free の対象なので費用はかからず、転送は数秒で完了します。

```
┌─────────────┐                  ┌──────────────────────┐                ┌─────────────┐
│   iCloud    │ ── IMAP IDLE ──► │  GCE e2-micro        │ ── Gmail API ──►│   Gmail     │
│  受信トレイ  │  (push / 常時接続) │  mail-transport      │ messages.insert │  受信トレイ  │
│  迷惑メール  │ ◄── BODY.PEEK[] ─│  + SQLite (同期位置)   │                │  迷惑メール  │
└─────────────┘                  └──────────────────────┘                └─────────────┘
```

## なぜ SMTP 転送ではないのか

通常のメール転送 (iCloud の「ルール」や自動転送) は、元の送信者を差出人としたまま
別のサーバーから再送信する形になります。その結果:

- **SPF** — 転送元サーバーは元ドメインの SPF レコードに載っていないので `fail`
- **DKIM** — 転送時にヘッダーや本文が書き換わると署名が壊れる
- **DMARC** — 上記により `p=reject` のドメインでは受信側が**受け取りを拒否**する

拒否された場合、Gmail 側には迷惑メールとしてすら残らず、**メールが消えます**。

本プロジェクトは配送経路を通らないので、この問題が原理的に発生しません。

| | SMTP 転送 | 本方式 (IMAP + Gmail API) |
|---|---|---|
| DMARC/SPF による消失 | 起きる | **起きない** |
| 迷惑メールの扱い | 消えることがある | **迷惑メールのまま届く** |
| 元の Date / ヘッダー | 変化する | **そのまま保持** |
| スパム再判定 | される | **されない** (指定ラベルが確定) |

`insert` は `import` と違いスパム分類器を通しません。そのため
「受信トレイ → 受信トレイ」「迷惑メール → 迷惑メール」が確実に再現されます。

---

## 1. 構成と費用

GCE の **e2-micro を 1 台**常時起動し、その上で常駐プロセスが IMAP IDLE で
新着を待ち受けます。同期位置は VM 上の SQLite に持ちます。

- **転送の遅延は数秒** — ポーリングではなく IMAP の push を使うため
- **Always Free の対象** — 費用がかからない (条件は下記)
- **他のサービスの無料枠を消費しない** — Cloud Run も Cloud Scheduler も
  Firestore も使いません。使うのは e2-micro 1 台と Secret Manager だけです

### GCE の Always Free について (2026-09 時点の確認結果)

- 対象は **e2-micro を 1 台**、リージョンは **us-west1 (オレゴン) / us-central1 (アイオワ) /
  us-east1 (サウスカロライナ)** のみ。他リージョンは通常課金です。
- **30 GB-月の標準永続ディスク (pd-standard)**。コンソールの既定は `pd-balanced` で、
  こちらは**無料枠の対象外**なので明示的に `pd-standard` を選ぶ必要があります。
- **北米からの下り通信 1 GB/月** (中国・オーストラリア宛を除く)。
- 無料枠の判定は「インスタンス数」ではなく**時間**で、その月の総時間ぶんまで無料。
  つまり 1 台を 24 時間 365 日動かし続けられます。
- 外部 IPv4 アドレスは 2024-02-01 から一般には $0.005/時 ですが、
  **無料枠には月の総時間ぶんの「使用中の外部 IP」が含まれます**。
  なお外部 IPv6 アドレスは無課金です。

> **注意**: 上記のうち外部 IP の扱いは、この環境から Google の公式ドキュメント
> (`docs.cloud.google.com`) に直接アクセスできなかったため、検索経由で得られた
> 公式ページの記述に基づいています。デプロイ後 2〜3 日は
> [課金レポート](https://console.cloud.google.com/billing) を確認し、
> 予算アラート (例: 1 ドル) を設定しておくことを強く勧めます。

Sources:
- [Google Cloud 無料プログラム / Compute Engine の無料枠](https://cloud.google.com/free/docs/free-cloud-features)
- [外部 IP アドレスの料金改定 (2024-02-01)](https://cloud.google.com/vpc/pricing-announce-external-ips)
- [VPC ネットワークの料金](https://cloud.google.com/vpc/network-pricing)

### 通信量の見積もり

- **iCloud からの取得は上り (受信) なので無料**です。GCP への内向き通信は課金されません。
- **Gmail API への挿入は googleapis.com 宛**で、GCP 内から Google API への通信は
  課金対象外とされています。仮に課金対象だとしても、転送するメールの総量が
  そのまま通信量になるため、月 1 GB の無料枠までは無料です
  (メール 1 通 100 KB として月 1 万通に相当)。
- 添付ファイルの多い環境で 1 GB を超えても、超過分は $0.12/GB 程度です。

---

## 2. 前提と準備するもの

| 必要なもの | 用途 |
|---|---|
| iCloud のアプリ用パスワード | IMAP 接続 (通常のパスワードでは接続不可) |
| Google アカウント (転送先の Gmail) | OAuth クライアントとリフレッシュトークン |
| GCP プロジェクト (課金有効) | Compute Engine / Secret Manager |
| `gcloud` CLI | デプロイ |

### 2-1. iCloud のアプリ用パスワードを発行する

iCloud メールは IMAP に対して OAuth を提供していないため、
**アプリ用パスワード (App-Specific Password)** が唯一の安全な選択肢です。
Apple ID のパスワード本体を使うより権限が限定され、いつでも個別に失効できます。

1. <https://account.apple.com/account/manage> にサインイン
2. 「サインインとセキュリティ」→「アプリ用パスワード」
3. 新規発行し、`xxxx-xxxx-xxxx-xxxx` 形式の文字列を控える

> 2 ファクタ認証が有効になっている必要があります。

### 2-2. Gmail API のリフレッシュトークンを取得する

1. GCP コンソール →「API とサービス」→「OAuth 同意画面」
   - User Type: **外部** (個人の Gmail の場合)
   - **公開ステータスを「本番環境」にする**
     - 「テスト」のままだと**リフレッシュトークンが 7 日で失効します**
     - `gmail.insert` は制限付きスコープのため未確認アプリの警告が出ますが、
       自分が作ったアプリを自分のアカウントで使う分には「詳細」→「移動」で続行できます
2. 「認証情報」→「OAuth クライアント ID を作成」→ 種別 **デスクトップ アプリ**
3. JSON をダウンロード
4. 手元の PC で実行:

```bash
pip install google-auth-oauthlib
python tools/get_gmail_refresh_token.py \
  --client-secret ~/Downloads/client_secret_xxx.json \
  --out deploy/gmail_oauth.json
```

要求するスコープは `https://www.googleapis.com/auth/gmail.insert` **のみ**です。
メールの閲覧・変更・削除・送信の権限は一切要求しません。

---

## 3. デプロイ

設定を書いたら、**あとは 1 コマンド**です。

```bash
cp deploy/config.env.example deploy/config.env
$EDITOR deploy/config.env        # PROJECT_ID と ICLOUD_USERNAME を書き換える

./deploy/provision.sh
```

`provision.sh` が次を順に実行します。**何度実行しても同じ結果になります**
(冪等) 。既にあるものは作り直さず、足りないものだけを作ります。

| | 内容 | 冪等性 |
|---|---|---|
| 1 | `gce/00-setup.sh` — API 有効化 / サービスアカウント / シークレット | 登録済みのシークレットは触りません。入れ替えるときは `--rotate-secrets` |
| 2 | `gce/10-create-vm.sh` — e2-micro を作成 | 既にあれば何もしません。無料枠から外れる設定は作成前に警告します |
| 3 | `ci/00-setup-wif.sh` — Workload Identity と **VM 単位の IAM** | `--skip-ci` で省略可 |
| 4 | `gce/20-deploy-app.sh` — コード転送と systemd 起動 | 毎回入れ替えて再起動します |

3 を VM 作成の**後**に実行しているのには理由があります。CI 用の
`roles/compute.osAdminLogin` と `roles/iap.tunnelResourceAccessor` は
インスタンスに紐づくので、**VM を作り直すと一緒に消えます**。
`provision.sh` はこの付け直しまで面倒を見ます。

主なオプション:

```bash
./deploy/provision.sh --skip-ci          # CI を使わない (WIF の設定を省く)
./deploy/provision.sh --rotate-secrets   # シークレットを入れ替える
./deploy/provision.sh --yes              # 確認を求めない
./deploy/provision.sh --help
```

アプリだけを更新したいときは `./deploy/gce/20-deploy-app.sh` を単独で流せます。

### VM を作り直す

```bash
./deploy/gce/90-delete-vm.sh                                   # 退避してから削除
./deploy/provision.sh --state-db deploy/state-backup/state.db  # 作り直して引き継ぐ
```

`90-delete-vm.sh` は削除前にサービスを止め、**同期位置 (`state.db`) を手元に
退避**します。これを `--state-db` で渡すと、作り直した VM が続きから転送を
再開します。渡さないと新しい VM は「起動時点より後のメール」しか転送せず、
停止していた間に届いたぶんを取りこぼします。

引き継ぎは **VM 側に `state.db` が無いときだけ**行われます。稼働中の VM に
`--state-db` 付きで流しても、動いている DB を上書きすることはありません
(上書きすると転送済みのメールを取り込み直してしまうため)。

削除するのは VM だけです。サービスアカウント・シークレット・
Workload Identity・ファイアウォール規則は残るので、作り直しは早く済みます。

動作確認:

```bash
# ログを追う
gcloud compute ssh mail-transport --zone=us-west1-b \
  --command='sudo journalctl -u mail-transport -f'

# 状態を見る
gcloud compute ssh mail-transport --zone=us-west1-b \
  --command='systemctl status mail-transport'
```

起動直後は「同期位置を初期化しました」「IDLE で新着を待機します」とだけ出て、
既存メールは転送されません (後述)。その後に届いたメールから転送が始まります。

### CI (GitHub Actions) からデプロイする

`main` への push で自動デプロイできます。サービスアカウントキー (JSON) を
GitHub に置く必要はありません。**Workload Identity 連携**で GitHub の OIDC
トークンを GCP に検証させるので、盗まれて困る長期の秘密情報が存在しません。

一度だけ、GCP 側の設定を行います (VM 作成後に実行してください):

```bash
./deploy/ci/00-setup-wif.sh
```

このスクリプトは次を行い、最後に GitHub に設定すべき値を出力します。

- Workload Identity プールと OIDC プロバイダの作成
  - **main ブランチの deploy.yml からの実行だけ**を信頼します (次項)
- デプロイ用サービスアカウントの作成と最小権限の付与
  - プロジェクト全体: `roles/compute.viewer` (読み取りのみ)
  - **この VM に限定**: `roles/compute.osAdminLogin` と `roles/iap.tunnelResourceAccessor`
  - 実行用 SA に限定: `roles/iam.serviceAccountUser`
- IAP 経由 SSH 用のファイアウォール規則 (`35.235.240.0/20` からの tcp:22 のみ)

出力された値を GitHub の Settings → Secrets and variables → Actions に登録します。

| 種別 | 名前 | 例 |
|---|---|---|
| Variables | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/123.../providers/github` |
| Variables | `GCP_DEPLOYER_SA` | `mail-transport-deployer@<project>.iam.gserviceaccount.com` |
| Variables | `GCP_PROJECT_ID` | `my-gcp-project` |
| Variables | `GCP_VM_NAME` | `mail-transport` |
| Variables | `GCP_VM_ZONE` | `us-west1-b` |
| Secrets | `ICLOUD_USERNAME` | `you@icloud.com` |

### なぜ main に限定するのか

プルリクエストにワークフローを 1 つ足すだけで本番に到達できてしまうため、
**GCP 側でブランチを縛る**必要があります。

- **fork からの PR** — トークンは読み取り専用でシークレットも渡らないので、
  そもそも `id-token: write` を取得できません。
- **同じリポジトリのブランチから出した PR** — 権限は制限されません。
  `id-token: write` を要求するワークフローを PR に含めれば、
  デプロイ用サービスアカウントに成り代われてしまいます。
  main が「PR 必須」で保護されていても、この経路はそれを迂回します。
- **`workflow_dispatch`** — 実行時に任意のブランチを選べます。

そこで OIDC プロバイダの `attribute-condition` を次のようにしています。
GitHub が署名した主張なので、ワークフロー側からは詐称できません。

```
assertion.repository == 'min-nano/mail-transport'
  && assertion.ref == 'refs/heads/main'
  && assertion.job_workflow_ref
       == 'min-nano/mail-transport/.github/workflows/deploy.yml@refs/heads/main'
```

| 実行元 | 結果 |
|---|---|
| main への push / main での `workflow_dispatch` | 許可 |
| PR (`ref` が `refs/pull/<番号>/merge` になる) | **拒否** |
| 他ブランチへの push、他ブランチでの `workflow_dispatch` | **拒否** |
| main に増えた別のワークフロー | **拒否** (deploy.yml に限定しているため) |
| 他リポジトリからのなりすまし | **拒否** |

`deploy.yml` の名前を変えるときは、この条件も直してください
(直さないと認証が通らなくなります)。

ワークフロー側にも保険を入れてあります。`id-token: write` は `deploy` ジョブ
だけに与え (テストのジョブには渡しません)、`if: github.ref == 'refs/heads/main'`
で main 以外では動かないようにしています。

**あわせて GitHub 側でも塞ぐことを勧めます。**
Settings → Environments → `production` → Deployment branches and tags で
Selected branches に `main` を追加すると、main 以外の ref では `deploy` ジョブ
自体が起動しなくなります。GCP 側と合わせて二重の防御になります。

以降、`src/` や `deploy/gce/` を変更して `main` に push すると
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) が動きます。

1. `ci.yml` を呼んでテスト・lint・パッケージ導入確認を行う (失敗したらデプロイしない)
2. Workload Identity で認証し、IAP トンネル越しに VM へ SSH
3. コードを転送してサービスを再起動
4. `systemctl is-active` を最大 30 秒ポーリングして稼働を確認。
   起動できなければ直近のログを出してジョブを失敗させる

Actions タブから手動実行 (`workflow_dispatch`) もでき、そのとき
`initial_import` に `all` を指定すれば既存メールの取り込みを走らせられます。

**デプロイ中の停止について**: 更新は「止めて入れ替えて起動」なので、数十秒
サービスが落ちます。その間に届いたメールは、再起動後の同期が前回の UID の
続きから取り込むため**失われません**。デプロイ同士は `concurrency` で直列化され、
走っているデプロイは中断されずに待たされます。

---

## 4. 即時性 (push) の仕組み

常駐モードでは、メールボックスごとに 1 本ずつ IMAP 接続を張り、
**RFC 2177 の IDLE** で待機します。iCloud 側に新着が入った瞬間に
`* n EXISTS` が飛んでくるので、ポーリングの待ち時間なしに同期が始まります。

- IMAP は 1 接続につき 1 メールボックスしか選択できないため、
  受信トレイと迷惑メールは別スレッド・別接続で待ち受けます。
- IDLE は 25 分ごとに張り直します (29 分以内という RFC 2177 の推奨に従うため。
  途中の NAT やサーバーに切断されるのを防ぎます)。
- 接続が切れたら指数バックオフで再接続し、**復帰時には必ず同期を 1 回走らせます**
  (切断中に届いたメールを取りこぼさないため)。
- IDLE の通知はサーバー都合で落ちることがあるので、`SAFETY_SYNC_SECONDS`
  (既定 5 分) ごとの定期同期を保険として併走させます。
- サーバーが IDLE に対応していない場合は自動的に `POLL_INTERVAL_SECONDS`
  ごとの定期同期に切り替わります。

`* OK Still here` のようなサーバーの keepalive では起こさないので、
無駄な同期は走りません。

---

## 5. 設定リファレンス

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `ICLOUD_USERNAME` | (必須) | iCloud メールアドレス |
| `ICLOUD_APP_PASSWORD` | (必須) | アプリ用パスワード。空白は自動で除去 |
| `ICLOUD_APP_PASSWORD_SECRET` | — | 上の代わりに Secret Manager のシークレット名を指定 |
| `ICLOUD_IMAP_HOST` | `imap.mail.me.com` | IMAP ホスト |
| `ICLOUD_IMAP_PORT` | `993` | IMAP ポート (implicit TLS) |
| `GMAIL_OAUTH_JSON` | (必須) | `client_id` / `client_secret` / `refresh_token` を含む JSON |
| `GMAIL_OAUTH_JSON_SECRET` | — | 上の代わりに Secret Manager のシークレット名を指定 |
| `GMAIL_USER_ID` | `me` | 挿入先ユーザー |
| `ROUTES` | 下記 | 転送経路の定義 (JSON) |
| `INITIAL_IMPORT` | `none` | `none`: 稼働後のメールのみ / `all`: 既存メールも全部 |
| `STATE_BACKEND` | `sqlite` | `sqlite` (ファイル) / `memory` (テスト用) |
| `STATE_DB_PATH` | `./state.db` | 状態ファイルの場所 (VM では `/var/lib/mail-transport/state.db`) |
| `IDLE_ENABLED` | `true` | `false` で IDLE を使わず定期ポーリングにする |
| `IDLE_REFRESH_SECONDS` | `1500` | IDLE を張り直す間隔 (29 分未満にすること) |
| `POLL_INTERVAL_SECONDS` | `60` | IDLE 非対応時のポーリング間隔 |
| `SAFETY_SYNC_SECONDS` | `300` | 保険の定期同期の間隔 |
| `DEBOUNCE_SECONDS` | `2` | 連続到着をまとめる待ち時間 |
| `MAX_MESSAGES_PER_RUN` | `40` | 1 回の同期で処理する上限 |
| `RUN_BUDGET_SECONDS` | `240` | 1 回の同期の時間上限。超えたら次回に持ち越す |
| `MAX_MESSAGE_BYTES` | `36700160` (35MiB) | これを超えるメールはスキップ |
| `SEEN_RETENTION_DAYS` | `30` | 重複排除レコードの保持日数 |
| `LOCK_TTL_SECONDS` | `540` | 多重起動防止ロックの有効期間 |
| `DRY_RUN` | `false` | `true` で挿入も状態保存も行わない |
| `LOG_LEVEL` | `INFO` | ログレベル |

### ROUTES の既定値

```json
[
  {"source": "INBOX",  "labels": ["INBOX"]},
  {"source": "\\Junk", "labels": ["SPAM"]}
]
```

- `source` — IMAP のメールボックス名、または `\Junk` のような
  **RFC 6154 の特殊用途フラグ**。迷惑メールフォルダの名前はロケールによって
  `Junk` だったり `迷惑メール` だったりするため、フラグ指定のほうが確実です。
- `labels` — Gmail のラベル ID。`INBOX` / `SPAM` / `STARRED` などのシステムラベル、
  またはユーザーラベルの ID (`Label_123` 形式)。

未読・スター状態は iCloud 側の `\Seen` / `\Flagged` から自動で引き継がれるので、
`UNREAD` / `STARRED` を `labels` に書く必要はありません。

アーカイブも転送したい場合の例:

```json
[
  {"source": "INBOX",   "labels": ["INBOX"]},
  {"source": "\\Junk",  "labels": ["SPAM"]},
  {"source": "Archive", "labels": ["Label_1234567890"]}
]
```

---

## 6. 動作の仕組み

1. **ロック取得** — 期限付きロックで同期の多重実行を防ぐ
2. **メールボックス解決** — `\Junk` などの特殊用途フラグを実際の名前に変換
3. **`SELECT` (読み取り専用)** — iCloud 側の既読状態を変化させない
4. **`UID SEARCH UID <last+1>:*`** — 前回位置より後ろの UID だけを列挙
5. **メタデータ一括取得** — サイズとフラグを先に見て、巨大メールをダウンロード前に除外
6. **`BODY.PEEK[]` で本文取得** — `\Seen` を立てずに RFC822 の生バイト列を取得
7. **重複排除** — `Message-ID` (なければ本文ハッシュ) で取り込み済みか確認
8. **`users.messages.insert`** — 元の `Date` ヘッダーを Gmail の日時に採用して挿入
9. **同期位置の更新** — **1 通ごとに**保存。途中で落ちても取りこぼし・二重取り込みを防ぐ

処理は「成功したところまで進める」設計です。ある 1 通の挿入に失敗した場合、
その UID より先には進まないので、次回の実行で必ず再試行されます。

### 初回起動時の挙動

既定 (`INITIAL_IMPORT=none`) では、**初回は同期位置を現在地に合わせるだけ**で
既存メールは転送しません。iCloud に数万通あるアカウントでいきなり全件取り込みが
走るのを防ぐためです。

既存メールもすべて取り込みたい場合は `deploy/config.env` の
`INITIAL_IMPORT="all"` にして `20-deploy-app.sh` を流し直します。
`MAX_MESSAGES_PER_RUN` ずつ処理されるので、通数によっては数時間かかります。
取り込みが終わったら `none` に戻しておくと、将来 UIDVALIDITY が変わったときの
再取り込みを防げます。

---

## 7. セキュリティ設計

- **サービスアカウントキー (JSON) を一切作成しません。**
  VM は自身にアタッチされたサービスアカウントの ID を使います。
  漏洩しうる長期鍵がディスク上に存在しません。
- **秘密情報は VM のディスクにも置きません。**
  `/etc/mail-transport.env` に書かれるのは Secret Manager の**シークレット名だけ**で、
  値はプロセス起動時に取得してメモリ上に保持します。
- **権限は最小限**。VM のサービスアカウントに与えるのは、
  該当する 2 つのシークレットに対する `secretAccessor` のみです。
  プロジェクト全体の権限は付与しません。
- **CI がデプロイできるのは main の `deploy.yml` からだけ。**
  OIDC トークンの発行条件をブランチとワークフローで縛っているので、
  プルリクエストにワークフローを足しても本番には届きません (第 3 章)。
- **Gmail のスコープは `gmail.insert` のみ。**
  トークンが漏れても、既存メールの閲覧・削除・送信はできません。
- **外部からの受信ポートを開けません。** VM は外向き通信しかしないので、
  追加のファイアウォール規則は不要です (SSH は既定の規則のまま)。
- **通信は常に TLS**。IMAP は 993 番の implicit TLS で、証明書検証は Python の
  既定コンテキスト (検証あり) を使います。
- **systemd で権限を絞っています。** 専用の非 root ユーザーで動作し、
  `ProtectSystem=strict` / `NoNewPrivileges` / `MemoryDenyWriteExecute` などを有効化。
  書き込みを許すのは状態ディレクトリだけです。
- VM は **Shielded VM** (セキュアブート / vTPM / 整合性監視) で作成し、
  SSH は **OS Login** を有効にします。

### 鍵を失効させたいとき

- iCloud: <https://account.apple.com/account/manage> でアプリ用パスワードを削除
- Gmail: <https://myaccount.google.com/permissions> でアプリのアクセス権を削除

### Google Workspace を使っている場合

ドメイン全体の委任 (Domain-Wide Delegation) を使えば、リフレッシュトークンを
保存せずにサービスアカウントだけで `gmail.insert` を実行できます。
保管すべき秘密情報が 1 つ減るため、Workspace ではそちらを推奨します。

---

## 8. 運用

### ログ

構造化 JSON で journald に出ます。

```bash
sudo journalctl -u mail-transport -f                    # 追いかける
sudo journalctl -u mail-transport --since '1 hour ago'  # 直近 1 時間
sudo journalctl -u mail-transport | grep 転送しました      # 転送実績
```

### 失敗に気づけるようにする

- 同期に失敗しても常駐プロセスは動き続け、次の契機で再試行します。
- プロセス自体が落ちた場合は systemd が 10 秒後に再起動します。
- 恒常的な失敗に気づくには、Cloud Monitoring の
  [稼働時間チェック](https://console.cloud.google.com/monitoring/uptime) ではなく、
  ログベースの指標 (`severity=ERROR`) にアラートを設定するのが確実です。

### 手動デプロイ

CI を使わずに更新する場合は、`deploy/gce/20-deploy-app.sh` をそのまま流します。
転送 → 再インストール → サービス再起動 → 稼働確認まで行います。

### ローカルでの確認

```bash
pip install -r requirements-dev.txt
cp .env.example .env && $EDITOR .env

# 何も転送せず、接続と検出だけ試す
PYTHONPATH=src python -m mailtransport.cli --dry-run

# 1 回だけ同期する
PYTHONPATH=src python -m mailtransport.cli

# 常駐して IDLE で待ち受ける (Ctrl-C で停止)
PYTHONPATH=src python -m mailtransport.cli --daemon
```

### テスト

```bash
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
```

lint は **`requirements-dev.txt` がピン留めした ruff** で実行してください。
ruff は版によって有効な規則が変わるため、`pip install ruff` で入れた別の版だと
手元で通っても CI で落ちることがあります。

### 依存の更新

[Dependabot](.github/dependabot.yml) が平日ごとに更新を確認し、
新しい版があればプルリクエストを作ります。対象は次の 2 つです。

- **pip** — `requirements.txt` (VM の実行時依存) と `requirements-dev.txt` (lint とテストの道具)
- **github-actions** — ワークフローで使っている action

プルリクエストでは `ci.yml` が走るので、lint・テスト・パッケージ導入確認が
通ったものだけを取り込めます。`requirements.txt` の更新を main に取り込むと、
`deploy.yml` が VM への再デプロイまで行います。

### Claude によるプルリクエストのレビュー

[`pr-review.yml`](.github/workflows/pr-review.yml) が、プルリクエストごとに
Claude にコードを読ませて指摘をコメントします。公式の
[`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action)
に載せています。

以前は [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) を直接叩く
Python (`tools/pr_review`) を持っていましたが、モデル・effort・ターン数・
費用上限・ツールの許可はすべて `claude_args` で同じことが書けるため、
保守する量の少ないほうに寄せました。

差分は `git diff base...head` (3 点) で書き出してから読ませます。base 側の
後続コミットが混ざらないようにするためで、これは以前と同じです。

結果は 1 つのコメントに集約されます。push のたびに新しいコメントを積まず、
アクションが自分のコメントを書き換えます (`use_sticky_comment` と
`track_progress`)。

#### 設定 — サブスクリプションの枠を使う

**API のクレジットは使いません。** 手元で次を実行して、サブスクリプション
(Pro / Max / Team / Enterprise) のトークンを発行します。

```bash
claude setup-token
```

出力されたトークンを Settings → Secrets and variables → Actions → Secrets に
`CLAUDE_CODE_OAUTH_TOKEN` として登録してください
([認証のドキュメント](https://code.claude.com/docs/en/authentication))。

未設定のあいだはレビューを行わず、警告を出して素通りします
(必須チェックではないので、設定前のプルリクエストを赤くしません)。
差分が空のときも同じく素通りします。

`anthropic_api_key` は**意図的に渡していません**。これを渡すと API のクレジットを
別枠で消費するためです。

> **消費するのはトークンを発行した人の枠です。** OAuth トークンは
> `claude setup-token` を実行した人のサブスクリプションに紐づきます。
> レビューの使用量はその人の利用枠から引かれ、対話で使う Claude Code と
> 同じ上限を共有します。大きなプルリクエストが続くと、手元の作業に
> 影響することがあります。

#### 安全側に倒していること

- **エージェントは読むだけ、ただし旧実装より一段弱くなっています。** 旧実装は
  `allowed_tools=["Read", "Glob", "Grep"]` でツール自体を絞り込んでいました。
  現在はアクションの既定を土台にしています。既定で渡るのは読み取り
  (`Glob` / `Grep` / `LS` / `Read`)、コメント反映用の `mcp__github_comment`、
  それに **git のコミット系** (`Bash(git add:*)` など) です。そのうえで:

  - `Edit` / `Write` / `NotebookEdit` は `--disallowedTools` で明示的に外しています
    (既定の許可に含まれないので、アクションが注入する規則と衝突しません)。
  - `WebSearch` / `WebFetch` はアクションが既定で拒否するので、読んだ内容を
    外に送る手段はありません。
  - git の書き込みは、ジョブの `permissions` が `contents: read` なので
    **push が通りません**。手元でのコミットは作れますが、リポジトリには残りません。
  - プロンプトでも「コードの変更やコミットはしない」と明示しています。

  > `--allowedTools` / `--disallowedTools` に bare の `Bash` を入れるのは避けてください。
  > アクションが注入する `Bash(git add:*)` などの許可規則と噛み合いません。

- **リポジトリの中身は指示ではなくデータとして扱わせています。** 「レビューを省略しろ」
  といった文がコードやコメントに混ざっていても従わず、そういう記述自体を
  指摘するよう `prompt` で指示しています。
- **fork からのプルリクエストでは動きません。** シークレットが渡らないためです
  (落として赤くするのではなく、そもそも起動しません)。
- **1 回のレビューに上限があります。** `--max-turns` と `--max-budget-usd` で
  頭打ちにしています。ただし `--max-turns` が SDK の設定 (`maxTurns`) に反映される
  ことはログで確認済みである一方、**`--max-budget-usd` と `--effort` が実際に
  効いているかは未確認です** (アクションは CLI ではなく Agent SDK を呼ぶため、
  対応する設定が無いフラグは無視される可能性があります)。

> **`CLAUDE_CODE_OAUTH_TOKEN` の露出について。** このワークフローは `pull_request`
> で走るため、**このリポジトリに push できる人はプルリクエストでワークフローを
> 書き換えてトークンを読み出せます**。GitHub Actions でシークレットを使う限り
> 避けられません (WIF のようにブランチで縛る仕組みが、この種のトークンには
> ありません)。読み出されるとサブスクリプションの枠を使われるため、
> 心配な場合は
> [`claude setup-token`](https://code.claude.com/docs/en/authentication)
> で発行し直せば古いトークンは使えなくなります。

---

## 9. トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `iCloud への IMAP ログインに失敗しました` | 通常のパスワードを使っている。アプリ用パスワードを発行し直す |
| `invalid_grant` で Gmail が失敗する | OAuth 同意画面が「テスト」のままでトークンが 7 日で失効した。「本番環境」に公開してトークンを取り直す |
| `メールボックスが見つかりません: \Junk` | iCloud 側に迷惑メールフォルダが無い (一度も迷惑メール判定がない新規アカウント)。1 通届けば自動で作られる |
| 既存メールが転送されない | 仕様。`INITIAL_IMPORT=all` にする (第 6 章) |
| 転送が遅い / 即時にならない | ログに「IDLE で新着を待機します」が出ているか確認。出ていなければ `SAFETY_SYNC_SECONDS` 間隔の定期同期にフォールバックしている |
| `監視接続が切れました` が続く | iCloud 側の一時障害か接続数上限。自動で再接続するが、頻発するなら `IDLE_ENABLED=false` でポーリングに切り替える |
| 同じメールが 2 通届く | `Message-ID` の無いメールが再取得された可能性。`SEEN_RETENTION_DAYS` を延ばす |
| 想定外の課金が出た | ディスクが `pd-standard` か、リージョンが us-west1/us-central1/us-east1 か、VM が 1 台だけかを確認 |
| CI が `GitHub の Variables / Secrets が未設定です` で落ちる | `deploy/ci/00-setup-wif.sh` の出力どおりに GitHub 側を登録する |
| CI の SSH が `Permission denied` になる | インスタンス単位の `roles/compute.osAdminLogin` が効かない場合がある。プロジェクトレベルで付与し直す |
| CI の SSH が IAP でつながらない | ファイアウォール規則 `allow-iap-ssh-mail-transport` と VM のタグ `mail-transport` を確認 |
| CI の認証が `unable to acquire impersonated credentials` などで失敗する | main 以外から動かしていないか確認。`deploy.yml` を改名・移動した場合は `deploy/ci/00-setup-wif.sh` を再実行して条件を更新する |

---

## 10. 制限事項

- **一方向・追記のみの同期です。** iCloud 側で後からメールを削除・移動・既読化しても
  Gmail には反映されません (取り込み時点の状態が入ります)。
- 対象は `ROUTES` に書いたメールボックスのみです。既定では受信トレイと迷惑メールだけで、
  送信済み・下書き・アーカイブは対象外です。
- `MAX_MESSAGE_BYTES` (既定 35MiB) を超えるメールはスキップされ、警告ログが出ます。
- IMAP の `UIDVALIDITY` がサーバー側で変わった場合、UID の対応関係が失われるため
  **現在位置から再開**します (全件再取り込みによる大量重複を避けるため)。
  この間に届いたメールを取りこぼす可能性があるので、警告ログを監視してください。
- 状態は VM のローカルディスク (SQLite) に持ちます。VM を作り直すと同期位置も
  失われ、次の起動はブートストラップからやり直しになります。
- Gmail 側のストレージ容量は消費します (転送ではなく実体の複製のため)。

## ライセンス

MIT

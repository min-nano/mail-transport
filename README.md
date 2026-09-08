# mail-transport — iCloud → Gmail メール転送

iCloud のメールを **SMTP を経由せず** Gmail に複製します。
iCloud からは **IMAP** で生のメッセージを取り出し、Gmail へは **Gmail API の
`users.messages.insert`** で直接書き込みます。

```
┌─────────────┐   IMAP (TLS)    ┌──────────────────┐   Gmail API    ┌─────────────┐
│   iCloud    │ ──────────────► │  Cloud Run       │ ─────────────► │   Gmail     │
│  受信トレイ  │   BODY.PEEK[]   │  (mail-transport)│ messages.insert│  受信トレイ  │
│  迷惑メール  │                 │                  │                │  迷惑メール  │
└─────────────┘                 └────────┬─────────┘                └─────────────┘
                                         │  ▲
                          同期位置/重複排除 │  │ 毎分 POST /sync (OIDC 認証)
                                         ▼  │
                                 ┌───────────┴────────┐
                                 │ Firestore │ Cloud Scheduler │
                                 └────────────────────┘
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

## 1. 前提と準備するもの

| 必要なもの | 用途 |
|---|---|
| iCloud のアプリ用パスワード | IMAP 接続 (通常のパスワードでは接続不可) |
| Google アカウント (転送先の Gmail) | OAuth クライアントとリフレッシュトークン |
| GCP プロジェクト (課金有効) | Cloud Run / Scheduler / Firestore / Secret Manager |
| `gcloud` CLI | デプロイ |

> 課金の有効化は必要ですが、後述の構成であれば**無料枠に収まります**。
> 想定外の課金を避けるため、予算アラート (例: 1 ドル) の設定を推奨します。

### 1-1. iCloud のアプリ用パスワードを発行する

iCloud メールは IMAP に対して OAuth を提供していないため、
**アプリ用パスワード (App-Specific Password)** が唯一の安全な選択肢です。
Apple ID のパスワード本体を使うより権限が限定され、いつでも個別に失効できます。

1. <https://account.apple.com/account/manage> にサインイン
2. 「サインインとセキュリティ」→「アプリ用パスワード」
3. 新規発行し、`xxxx-xxxx-xxxx-xxxx` 形式の文字列を控える

> 2 ファクタ認証が有効になっている必要があります。

### 1-2. Gmail API のリフレッシュトークンを取得する

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

## 2. デプロイ

```bash
cp deploy/config.env.example deploy/config.env
$EDITOR deploy/config.env        # PROJECT_ID と ICLOUD_USERNAME を書き換える

./deploy/00-setup.sh             # API 有効化 / Firestore / SA / シークレット登録
./deploy/10-deploy.sh            # Cloud Run デプロイ + Cloud Scheduler 作成
./deploy/20-firestore-ttl.sh     # 任意: 重複排除レコードの自動削除
```

動作確認:

```bash
# 1 回だけ手動実行
gcloud scheduler jobs run mail-transport-sync --location=asia-northeast1

# ログを見る
gcloud run services logs read mail-transport --region=asia-northeast1 --limit=50
```

初回は「同期位置を初期化しました」とだけ出て、メールは転送されません (後述)。
その後に届いたメールから転送が始まります。

---

## 3. 即時性 (push) について

要件は「できるだけ即時」でしたが、**無料枠との両立を優先して毎分ポーリング**を
既定にしています。理由と代替案は次のとおりです。

### 採用: Cloud Scheduler で毎分ポーリング (既定)

- 遅延: **平均 30 秒 / 最大 60 秒**
- 費用: **無料枠内**

### 見送り: IMAP IDLE による push

IMAP の push (IDLE) は TCP 接続を張り続ける必要があります。Cloud Run で常時起動
(`--min-instances=1`) にすると、月あたり約 260 万 vCPU 秒を消費し、
無料枠 (18 万 vCPU 秒/月) を**桁違いに超過**します。
GCE の `e2-micro` 無料枠を使う手はありますが、対象リージョンが US に限られ、
可用性の管理も自前になるため採用していません。

`IDLE` 前提で常時起動したい場合、本アプリは HTTP サービスなので
`while true; do curl .../sync; sleep 30; done` 相当を外から回すだけで動きます。

### 見送り: iCloud のルールでトリガーする

iCloud のルールは「転送」「フォルダ移動」「削除」しかできず、Webhook を叩けません。
「Gmail に転送」させると本プロジェクトが解決している DMARC の問題が再発します。

どうしても秒単位が必要な場合の発展形として、
*iCloud ルールで通知専用アドレスに転送 → Cloudflare Email Workers 等で受信 →
`/sync` を叩く* という構成は可能です。ただし外部サービスに GCP の呼び出し権限を
渡すことになり、認証面が弱くなるため既定では採用していません。
その場合も本文は IMAP から取得するので、DMARC で通知が消えても
最悪 1 分後のポーリングで確実に届きます。

### 間隔を変える

```bash
# deploy/config.env の SCHEDULE を書き換えて再デプロイ
SCHEDULE="*/2 * * * *"   # 2 分ごと (無料枠に余裕を持たせたい場合)
```

---

## 4. 無料枠の見積もり

毎分実行 = 月 43,200 回、1 回あたり実行 3 秒 (メール 0 通時) として:

| サービス | 使用量 | 無料枠 | 判定 |
|---|---|---|---|
| Cloud Run vCPU | 約 129,600 vCPU 秒 | 180,000 vCPU 秒/月 | ○ |
| Cloud Run メモリ | 約 64,800 GiB 秒 | 360,000 GiB 秒/月 | ○ |
| Cloud Run リクエスト | 43,200 | 200 万/月 | ○ |
| Cloud Scheduler | ジョブ 1 個 | 3 個/月 | ○ |
| Firestore 読み取り | 約 4,300/日 | 50,000/日 | ○ |
| Firestore 書き込み | 約 3,000/日 | 20,000/日 | ○ |
| Artifact Registry | 約 0.2 GB | 0.5 GB | ○ |
| Cloud Logging | 数十 MB | 50 GiB/月 | ○ |

ポイント:

- **CPU は「リクエスト処理中のみ割り当て」(既定) のままにする。**
  `--min-instances` を 1 以上にすると無料枠を超えます。
- 1 回の実行が平均 4 秒を超えると vCPU 枠に届きます。メール量が多い環境や
  余裕を持たせたい場合は `SCHEDULE` を 2 分間隔にしてください。
- 毎分アクセスがあるためインスタンスは温まったままになり、Secret Manager の
  アクセス回数 (無料 1 万回/月) もコールドスタート時のみで収まります。
- 古いコンテナイメージは Artifact Registry の枠を食うので、時々削除してください。

---

## 5. 設定リファレンス

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `ICLOUD_USERNAME` | (必須) | iCloud メールアドレス |
| `ICLOUD_APP_PASSWORD` | (必須) | アプリ用パスワード。空白は自動で除去 |
| `ICLOUD_IMAP_HOST` | `imap.mail.me.com` | IMAP ホスト |
| `ICLOUD_IMAP_PORT` | `993` | IMAP ポート (implicit TLS) |
| `GMAIL_OAUTH_JSON` | (必須) | `client_id` / `client_secret` / `refresh_token` を含む JSON |
| `GMAIL_USER_ID` | `me` | 挿入先ユーザー |
| `ROUTES` | 下記 | 転送経路の定義 (JSON) |
| `INITIAL_IMPORT` | `none` | `none`: 稼働後のメールのみ / `all`: 既存メールも全部 |
| `MAX_MESSAGES_PER_RUN` | `40` | 1 回の実行で処理する上限 |
| `RUN_BUDGET_SECONDS` | `240` | 実行時間の上限。超えたら次回に持ち越す |
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

1. **ロック取得** — Firestore の期限付きロックで多重起動を防ぐ
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

既存メールもすべて取り込みたい場合:

```bash
gcloud run services update mail-transport --region=asia-northeast1 \
  --update-env-vars=INITIAL_IMPORT=all
```

`MAX_MESSAGES_PER_RUN` ずつ処理されるので、通数によっては数時間かかります。
取り込みが終わったら `INITIAL_IMPORT=none` に戻しておくと、
将来 UIDVALIDITY が変わったときの再取り込みを防げます。

---

## 7. セキュリティ設計

- **サービスアカウントキー (JSON) を一切作成しません。**
  Cloud Run はアタッチされたサービスアカウントの ID を、Cloud Scheduler は
  OIDC トークンを使います。漏洩しうる長期鍵がありません。
- **Cloud Run は非公開** (`--no-allow-unauthenticated`)。
  `roles/run.invoker` を持つ Scheduler 用サービスアカウントだけが呼べます。
  アプリ側に共有シークレットを持たせないので、鍵の更新も不要です。
- **権限は分離**。実行用 SA には Firestore (`roles/datastore.user`) と
  該当シークレットの読み取りのみ。呼び出し用 SA には invoker のみ。
- **Gmail のスコープは `gmail.insert` のみ。**
  トークンが漏れても、既存メールの閲覧・削除・送信はできません。
- **シークレットは Secret Manager**。Cloud Run の `--set-secrets` で注入され、
  コンテナイメージにも環境変数の平文設定にも残りません。
- **通信は常に TLS**。IMAP は 993 番の implicit TLS で、証明書検証は Python の
  既定コンテキスト (検証あり) を使います。
- コンテナは **非 root** ユーザーで動作します。

### 鍵を失効させたいとき

- iCloud: <https://account.apple.com/account/manage> でアプリ用パスワードを削除
- Gmail: <https://myaccount.google.com/permissions> でアプリのアクセス権を削除

### Google Workspace を使っている場合

ドメイン全体の委任 (Domain-Wide Delegation) を使えば、リフレッシュトークンを
保存せずにサービスアカウントだけで `gmail.insert` を実行できます。
保管すべき秘密情報が 1 つ減るため、Workspace ではそちらを推奨します。

---

## 8. 運用

### ログを見る

```bash
gcloud run services logs read mail-transport --region=asia-northeast1 --limit=100
```

ログは構造化 JSON なので、Cloud Logging で絞り込めます:

```
resource.type="cloud_run_revision"
jsonPayload.message="メールを転送しました"
```

### 失敗に気づけるようにする

`/sync` は経路のいずれかが失敗すると **HTTP 500** を返します。
Cloud Monitoring で「Cloud Scheduler ジョブの失敗」または
「Cloud Run の 5xx」にアラートを設定しておくと、転送が止まったことに気づけます。

### ローカルでの確認

```bash
pip install -r requirements-dev.txt
cp .env.example .env && $EDITOR .env

# 何も転送せず、接続と検出だけ試す
PYTHONPATH=src python -m mailtransport.cli --dry-run

# 実際に転送する (Firestore への認証が必要)
PYTHONPATH=src python -m mailtransport.cli
```

### テスト

```bash
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
```

---

## 9. トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `iCloud への IMAP ログインに失敗しました` | 通常のパスワードを使っている。アプリ用パスワードを発行し直す |
| `invalid_grant` で Gmail が失敗する | OAuth 同意画面が「テスト」のままでトークンが 7 日で失効した。「本番環境」に公開してトークンを取り直す |
| `メールボックスが見つかりません: \Junk` | iCloud 側に迷惑メールフォルダが無い (一度も迷惑メール判定がない新規アカウント)。1 通届けば自動で作られる |
| 既存メールが転送されない | 仕様。`INITIAL_IMPORT=all` にする (第 6 章) |
| 同じメールが 2 通届く | `Message-ID` の無いメールが再取得された可能性。`SEEN_RETENTION_DAYS` を延ばす |
| 転送が遅れる | `MAX_MESSAGES_PER_RUN` を超えて溜まっている。ログの `remaining` を確認する |
| Cloud Run が 500 を返す | ログの `status: "error"` と `routes[].error` を確認 |

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
- Gmail 側のストレージ容量は消費します (転送ではなく実体の複製のため)。

## ライセンス

MIT

# mail-transport — iCloud → Gmail メール転送

iCloud のメールを **SMTP を経由せず** Gmail に複製します。
iCloud からは **IMAP** で生のメッセージを取り出し、Gmail へは **Gmail API の
`users.messages.insert`** で直接書き込みます。

推奨構成は **GCE の e2-micro を常時起動し、IMAP IDLE で新着を待ち受ける**方式です。
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

## 1. 構成の選択

| | 構成 A: **GCE e2-micro** (推奨) | 構成 B: Cloud Run + Scheduler |
|---|---|---|
| 転送の遅延 | **数秒** (IMAP IDLE の push) | 平均 30 秒 / 最大 60 秒 |
| 費用 | Always Free (後述) | 無料枠内だが**枠を大きく消費する** |
| 消費する無料枠 | GCE の e2-micro 1 台ぶんのみ | Cloud Run の CPU 時間の約 7 割 + Scheduler 1 ジョブ + Firestore |
| 状態の保存先 | VM 上の SQLite | Firestore |
| 運用 | VM 1 台の面倒を見る | フルマネージド |

同じプロジェクトで他に Cloud Run を使っている場合、構成 B は共有の無料枠を
食い合います。構成 A なら Cloud Run にも Scheduler にも Firestore にも一切
触れないので、他のサービスに影響しません。

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

## 3. デプロイ (構成 A: GCE e2-micro)

```bash
cp deploy/config.env.example deploy/config.env
$EDITOR deploy/config.env        # PROJECT_ID と ICLOUD_USERNAME を書き換える

./deploy/gce/00-setup.sh         # API 有効化 / サービスアカウント / シークレット登録
./deploy/gce/10-create-vm.sh     # e2-micro を作成 (無料枠から外れる設定は警告)
./deploy/gce/20-deploy-app.sh    # コード転送 + systemd サービスとして起動
```

アプリを更新したいときは `20-deploy-app.sh` をもう一度流すだけです
(転送 → 再インストール → サービス再起動まで行います)。

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

### 構成 B: Cloud Run + Cloud Scheduler

Cloud Run 側の無料枠に余裕があり、VM の管理をしたくない場合はこちらも使えます。

```bash
./deploy/cloudrun/00-setup.sh
./deploy/cloudrun/10-deploy.sh
./deploy/cloudrun/20-firestore-ttl.sh   # 任意
```

毎分ポーリング (43,200 回/月、1 回 3 秒として約 129,600 vCPU 秒) で、
Cloud Run の無料枠 180,000 vCPU 秒のうち **7 割強を消費します**。
同じ請求先アカウントで他に Cloud Run を使うなら、`SCHEDULE` を
`*/5 * * * *` などに緩めるか、構成 A を選んでください。
`--min-instances` は必ず 0 のままにしてください (1 以上で無料枠を大きく超えます)。

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
| `STATE_BACKEND` | `sqlite` | `sqlite` / `firestore` / `memory` |
| `STATE_DB_PATH` | `./state.db` | SQLite の保存先 (VM では `/var/lib/mail-transport/state.db`) |
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

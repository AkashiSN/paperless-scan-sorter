# TrueNAS SCALE への導入手順

Paperless-ngx 一式と ai-classifier を、TrueNAS SCALE の **Apps → Custom App** に 1 つのアプリとして置き、スキャナーからの取り込み・スナップショット・Drive への同期までを整える手順です。構成の理由は [`design.md`](design.md) の 9・10 章にあります。TrueNAS を使わずに Docker Compose で動かす手順は [`setup.md`](setup.md) です。

画面の項目名は TrueNAS の版によって少し違うことがあります。以下の `<...>` は環境ごとの値に置き換えてください。

- [1. 用意するもの](#1-用意するもの)
- [2. データセットを作る](#2-データセットを作る)
- [3. ユーザーと権限](#3-ユーザーと権限)
- [4. SMB 共有を作る](#4-smb-共有を作る)
- [5. Custom App として置く](#5-custom-app-として置く)
- [6. Paperless を準備する](#6-paperless-を準備する)
- [7. ai-classifier を動かす](#7-ai-classifier-を動かす)
- [8. スキャナーを設定する](#8-スキャナーを設定する)
- [9. スナップショットと書き出し](#9-スナップショットと書き出し)
- [10. Google Drive への同期](#10-google-drive-への同期)
- [11. 更新する](#11-更新する)

---

## 1. 用意するもの

| | |
| --- | --- |
| TrueNAS SCALE | Apps（Docker）と Custom App の Install via YAML が使えること |
| プール | 書類を置くプール（以下 `<pool>`） |
| スキャナー | ネットワークフォルダー（SMB）に直接保存できるもの |
| Claude | サブスクリプションプランのアカウント。`claude setup-token` を実行できる端末 |
| Google アカウント | Drive に同期するなら |

## 2. データセットを作る

**Datasets** で、`<pool>` の下に `paperless` を作り、その下に次の 6 つを作ります。どれもスナップショットの対象になるよう、同じ `paperless` の下にまとめます。

| データセット | 用途 | 作るときのプリセット |
| --- | --- | --- |
| `<pool>/paperless/consume` | 取り込み口。SMB 共有にもする | SMB |
| `<pool>/paperless/media` | 原本・アーカイブ版・サムネイル | Generic |
| `<pool>/paperless/data` | 検索インデックスなど | Generic |
| `<pool>/paperless/pgdata` | PostgreSQL のデータ | Generic |
| `<pool>/paperless/export` | `document_exporter` の出力先 | Generic |
| `<pool>/paperless/classifier` | ai-classifier の状態（失敗回数、退避した PDF など） | Generic |

以降、`<root>` は `/mnt/<pool>/paperless` を指します。

## 3. ユーザーと権限

**Credentials → Users** で、次の 2 人を用意します。

- **Paperless の実行ユーザー**（以下 `<paperless-uid>` / `<paperless-gid>`）: Paperless のコンテナがこの UID/GID で動きます（`USERMAP_UID` / `USERMAP_GID`）。既存のユーザーを使っても、専用に作っても構いません
- **スキャナー用のユーザー**（以下 `<scanner-user>`）: SMB でだけ使う。SMB 以外のログインは要りません

TrueNAS のシェル（**System → Shell**）で所有者を合わせます。

```bash
chown -R <paperless-uid>:<paperless-gid> <root>/media <root>/data <root>/export
# ai-classifier はコンテナの中で UID/GID 1000 として動く
chown 1000:1000 <root>/classifier
```

`pgdata` は PostgreSQL のコンテナが初回の起動時に自分で所有者を決めるので、触りません。

`consume` は ACL で権限を付けます（**Datasets → consume → Permissions → Edit**）。

- `<scanner-user>`: 書き込み（ファイルとフォルダーの作成）
- `<paperless-uid>` のユーザー: 読み取りと削除（取り込んだ後にファイルを消すため）

## 4. SMB 共有を作る

**Shares → SMB → Add** で、`<root>/consume` を共有名 `scans-inbox` で公開します。

- SMB1 は使わない（TrueNAS の既定で無効）
- 共有の中に `private/` フォルダーを作っておく。Claude に送りたくない書類（マイナンバー・医療関係など）の保存先です（[`design.md`](design.md) 10 章）

同じデータセットを SMB 共有とアプリのホストパスの両方に使うと、Apps のインストール時にホストパスの安全確認で止められることがあります。そのときは **Apps → Configuration → Settings** の「Host Path Safety Checks」を外します。

## 5. Custom App として置く

[`compose.example.yaml`](../compose.example.yaml) を **Apps → Discover Apps → Custom App → Install via YAML** に貼り付けます。アプリ名は `paperless` などにします。

**Install via YAML は `.env` を読みません。** 貼り付けた YAML の `${...}` を、次の値に置き換えてください。

| 置き換える箇所 | 入れる値 |
| --- | --- |
| `${PAPERLESS_ROOT}` | `<root>`（例: `/mnt/<pool>/paperless`） |
| `${PAPERLESS_PORT}` | Web UI を出すホスト側のポート。LAN だけに出すなら `"<LAN 側の IP>:<ポート>:8000"` の形にする |
| `${TZ}` | 利用者のタイムゾーン（IANA 名。例: `Asia/Tokyo`）。2 か所 |
| `${DB_PASSWORD}` | 乱数で作った長い文字列（`openssl rand -base64 48` など）。2 か所とも同じ値 |
| `${PAPERLESS_SECRET_KEY}` | 乱数で作った長い文字列 |
| `${USERMAP_UID}` / `${USERMAP_GID}` | 手順 3 の `<paperless-uid>` / `<paperless-gid>` |
| `${PAPERLESS_TOKEN}` | いったん空の文字列 `""`。手順 7 で入れる |
| `${CLAUDE_CODE_OAUTH_TOKEN}` | いったん空の文字列 `""`。手順 7 で入れる |
| `${INTERVAL:-300}` など既定値つきのもの | そのまま既定値（`300`、`sonnet`、`300`）を書く |

Paperless のイメージは配置例では `:latest` です。運用では版を固定します（例: `ghcr.io/paperless-ngx/paperless-ngx:2.x.y`）。ai-classifier のイメージ `ghcr.io/akashisn/paperless-scan-sorter:latest` は main の先頭を指すので、固定したいときは `:sha-1234567` のようにコミットのタグにします。

アプリ画面に「Web UI」ボタンを出したいときは、**貼り付ける前に**トップレベルに `x-portals` を足します。あとから YAML を編集して足しても、ボタンは出ません（Custom App の portals は作成時にしか読まれません）。

```yaml
x-portals:
  - name: Web UI
    scheme: http
    host: <TrueNAS の IP かホスト名>
    port: <PAPERLESS_PORT に入れたポート>
    path: /
```

保存すると、Paperless・PostgreSQL・Redis が起動します。ai-classifier は Paperless が応答するまで待ち、トークンが空なので終了コード 2 で止まって再起動を繰り返します。これは手順 7 でトークンを入れるまでの想定どおりの動きです。

## 6. Paperless を準備する

TrueNAS のシェルで管理者ユーザーを作ります。コンテナ名は `docker ps` で確かめてください（Custom App では `ix-<アプリ名>-paperless-1` のような名前になります）。

```bash
docker ps --format '{{.Names}}' | grep paperless
docker exec -it <paperless のコンテナ名> python3 manage.py createsuperuser
```

続けて、[`setup.md`](setup.md) の「2. Paperless の準備」をそのまま行います。

- 状態管理用のタグ 7 つ（`inbox` `page-todo` `page-review` `ai-todo` `ai-done` `ai-review` `ai-failed`）
- 取り込み時のワークフロー（`inbox` と、`ai-todo` または `page-todo` を付ける。`private/` に一致しないパスに限る）
- ai-classifier 用のユーザーと API トークン

## 7. ai-classifier を動かす

ブラウザのある端末で `claude setup-token` を実行し、トークンを発行します（[`setup.md`](setup.md) の「3. Claude のトークンを発行する」）。

**Apps → paperless → Edit** で YAML を開き、ai-classifier の `PAPERLESS_TOKEN` と `CLAUDE_CODE_OAUTH_TOKEN` に値を入れて保存します。トークンはアプリの設定に保存され、イメージには焼き込まれません。

ai-classifier のログ（**Apps → paperless → Logs** で ai-classifier を選ぶ）に、周期ごとに次の 2 行が出れば動いています。

```text
ページ整形: fixed=… unchanged=… review=… aborted=…
周期完了: done=… review=… retried=… failed=… aborted=…
```

`aborted=True` が続くときや終了コードの意味は、[`setup.md`](setup.md) の「4. 1 周期だけ動かして確かめる」を見てください。

## 8. スキャナーを設定する

スキャナーの管理画面（ScanSnap iX シリーズなら ScanSnap Home）で、ネットワークフォルダーへの保存プロファイルを作ります。

| 項目 | 値 |
| --- | --- |
| 保存先 | `\\<TrueNAS の IP かホスト名>\scans-inbox` |
| ユーザー | `<scanner-user>` |
| ファイル形式 | PDF。**検索可能 PDF にはしない**（OCR は Paperless に任せる） |
| 回転 | 自動（使える機種なら） |
| 白紙ページの削除 | 有効（使える機種なら） |
| 重送検出 | 有効 |

Claude に送りたくない書類用に、保存先を `\\<…>\scans-inbox\private` にした別のプロファイルも作ります。テスト用の PDF を `private/` に保存して、`ai-todo` も `page-todo` も付かないことを確かめてください。

スキャナーが NTP に到達できることも確かめます（ファイルの日時がずれるため）。

## 9. スナップショットと書き出し

**Data Protection → Periodic Snapshot Tasks → Add** で、`<pool>/paperless` を**再帰的に**、毎日、30 日保持にします。

書類とメタデータの一式は、週に 1 回 `document_exporter` で `export` に書き出します。**System → Advanced → Cron Jobs → Add** で、root として週 1 回、次を実行します。

```bash
docker exec <paperless のコンテナ名> document_exporter ../export
```

スナップショットから原本のファイルだけを戻すと、Paperless のデータベースと食い違います。戻し方は [`design.md`](design.md) の 6.2 を見てください。

## 10. Google Drive への同期

整理済みのアーカイブ版を Drive に同期します。閲覧用で、バックアップとしては扱いません（[`design.md`](design.md) 10 章）。

1. **Credentials → Backup Credentials → Cloud Credentials → Add** で Google Drive を追加する
2. **Data Protection → Cloud Sync Tasks → Add** で次のように作る

| 項目 | 値 |
| --- | --- |
| 向き | PUSH |
| 転送モード | SYNC（Paperless 側の削除・移動も反映する） |
| 同期元 | `<root>/media/documents/archive` |
| 同期先 | Drive 上の専用フォルダー |
| 頻度 | 毎日 |

## 11. 更新する

イメージのタグを書き換えて保存すると、そのコンテナが新しいイメージで作り直されます。版を固定していれば、**Apps → paperless → Edit** でタグを新しいものにして保存するだけです。

- ai-classifier: ghcr の Packages（`paperless-scan-sorter`）で新しい `sha-` のタグを確かめる
- Paperless: リリースノートを読んでから上げる。上げる前にスナップショットを取る

Paperless を上げたときは、ページ整形が使う `edit_pdf` と版の扱いが変わっていないかを、テスト用の書類 1 件で確かめてください（[`design.md`](design.md) 6.3）。

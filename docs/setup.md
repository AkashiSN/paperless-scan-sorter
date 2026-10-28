# セットアップ

Paperless-ngx と ai-classifier を Docker Compose で動かし、最初の書類を分類するまでの手順。構成の理由は [`design.md`](design.md) の 5・8・9・10 章にある。

## 1. 配置例を用意する

```bash
cp .env.example .env
```

`.env` に次を入れる。`<pool>` は環境ごとのプール名に読み替える。

- `PAPERLESS_ROOT`: データ置き場（例: `/mnt/<pool>/paperless`）
- `PAPERLESS_SECRET_KEY`、`DB_PASSWORD`: 乱数で作った長い文字列
- `TZ`: 利用者のタイムゾーン（IANA 名）。Paperless と ai-classifier の両方に渡す。ai-classifier は `created` の範囲（未来日でないか）をこのタイムゾーンの今日で判定する
- `PAPERLESS_PORT`: Paperless の Web UI を公開するホスト側のポート。compose の `ports` は既定で全インターフェースに公開する。LAN の中だけで使うなら `"<LAN 側の IP>:${PAPERLESS_PORT}:8000"` のように LAN 側のアドレスに限って公開し、インターネットへは直接出さない
- `USERMAP_UID`、`USERMAP_GID`: `consume` の ACL で読み取りと削除を許可したユーザー

`PAPERLESS_TOKEN` と `CLAUDE_CODE_OAUTH_TOKEN` は、手順 3 と 4 で発行してから入れる。`.env` は追跡しない。

`PAPERLESS_ROOT` の下に `consume`・`media`・`data`・`export`・`pgdata`・`classifier` を作る。ai-classifier はコンテナ内で UID/GID 1000 として動くので、`classifier` はそのユーザーが書き込めるようにする。

`.env` を編集してもシェルの変数にはならないので、`PAPERLESS_ROOT` の行だけを `.env` から読み出し、空・`/`・プレースホルダーのままの値を拒んでからディレクトリを作る。`.env` を `.` や `source` でシェルに読み込まない。`<pool>` が残っていると `<` と `>` がリダイレクトとして実行され、root のシェルではルート直下にファイルを作ってしまう。`.env` の `PAPERLESS_ROOT` は引用符で囲まずに書く。bash と zsh のどちらにもそのまま貼り付けられる。

```bash
PAPERLESS_ROOT=$(sed -n 's/^PAPERLESS_ROOT=//p' .env | tail -n 1)
: "${PAPERLESS_ROOT:?PAPERLESS_ROOT を .env に設定する}"
case "$PAPERLESS_ROOT" in
  *'<'* | *'>'*) echo "PAPERLESS_ROOT のプレースホルダー（<pool> など）を書き換える: $PAPERLESS_ROOT" >&2 ;;
  /*[!/]*)
    mkdir -p "$PAPERLESS_ROOT"/{consume,media,data,export,pgdata,classifier} &&
      chown 1000:1000 "$PAPERLESS_ROOT/classifier" ;;
  *) echo "PAPERLESS_ROOT は / 以外の絶対パスにする: '$PAPERLESS_ROOT'" >&2 ;;
esac
```

Paperless だけ先に起動し、管理者ユーザーを作る。

```bash
docker compose -f compose.example.yaml up -d paperless
docker compose -f compose.example.yaml exec paperless python3 manage.py createsuperuser
```

## 2. Paperless の準備

Web UI（`http://<ホスト>:<PAPERLESS_PORT>`）で管理者としてログインして行う。

**状態管理用のタグを作る。** ai-classifier はタグを作らない（[D-005](decisions.md)）ので、次の 7 つを手で作り、名前を一字一句そろえる。無いと ai-classifier は起動時に止まる。

`inbox` `page-todo` `page-review` `ai-todo` `ai-done` `ai-review` `ai-failed`

自動マッチは「なし」にする。

**取り込み時のワークフローを作る。** トリガーは「書類が追加された」、アクションは「タグを割り当てる」で `inbox` と `ai-todo` を付ける。

ページ整形（設計書 6.3）を使うときは、`ai-todo` の代わりに `page-todo` を付ける。ai-classifier が白紙・重複・向きを直してから `ai-todo` に付け替える。ページ整形の `edit_pdf` は、書類の所有者が無いか ai-classifier のユーザー自身であることを求める。ワークフローで所有者を設定しないか、所有者を ai-classifier のユーザーにする。条件に合わない書類は直されず、`page-review` が付いて分類へ進む。

**`private/` を除外する。** マイナンバーや医療関係など Claude に送りたくない書類は、スキャナーの別プロファイルで `scans-inbox/private/` に保存する。ワークフローの `filter_path` はワイルドカードの一致だけで否定を書けないので、`ai-todo` を付けるワークフローは通常のスキャンだけに一致するパスにする。テスト用の PDF を `private/` に置き、`ai-todo` が付かないことを必ず確かめる。

**ai-classifier 用のユーザーを作る。** 管理者とは別に、次の権限だけを持つユーザーを作る。

- 書類: 閲覧・変更
- タグ: 閲覧・変更（追加は与えない）
- 発行元、文書タイプ: 閲覧・変更・追加

書類に所有者が設定されている場合は、そのユーザーが書類を閲覧・変更できる権限を持つことを確かめる。

**API トークンを発行する。** 作ったユーザーでログインし、「プロフィール」の API 認証トークンを生成して、`.env` の `PAPERLESS_TOKEN` に入れる。

## 3. Claude のトークンを発行する

ブラウザのある端末で Claude Code を入れ、サブスクリプションプランのアカウントでログインして実行する。

```bash
claude setup-token
```

出たトークンを `.env` の `CLAUDE_CODE_OAUTH_TOKEN` に入れる。有効期限は約 1 年なので、日付を控えて期限前に再発行する。

書類の本文は Claude（Anthropic）に送られる。claude.ai の「プライバシー」設定で、会話をモデルの改善に使う設定（「Help improve Claude」）がオフであることを確かめる。

## 4. 1 周期だけ動かして確かめる

`ai-todo` の付いた書類を 1 件取り込ませてから、1 周期だけ回す。

```bash
docker compose -f compose.example.yaml run --rm ai-classifier --once
```

1 周期ではページ整形を先に回し、続けて分類を回す。ログに `ページ整形: fixed=… unchanged=… review=… aborted=…` と `周期完了: done=… review=… retried=… failed=… aborted=…` が出れば 1 周期が終わっている。`page-todo` の書類が無ければ、ページ整形の数はすべて 0 になる。

- 終了コード 2: 環境変数の不備。ログに出るのは変数名だけ（値は出さない）。`PAPERLESS_URL`・`PAPERLESS_TOKEN`・`CLAUDE_CODE_OAUTH_TOKEN` を確かめる
- 終了コード 1: 状態管理用のタグが足りない。ログに足りないタグ名が出る。`--once` では、ページ整形か分類の周期で想定外の例外が出たときも 1 で終わる（ログに例外が出る。片方が例外で止まっても、もう片方は回す）。常駐ループでは、タグ不足のときだけ 1 で止まり、想定外の例外はログに出して次の周期へ進む
- `aborted=True` でその周期が打ち切られている: 利用枠の上限、認証エラー、ネットワーク障害のどれか。書類は `ai-todo` のまま残る。ログを見て、トークンと接続先を確かめる
- ページ整形の `aborted=True`: Paperless に繋がらない、認証エラー、`/state/originals/` に書けない、のどれか。書類は `page-todo` のまま残る。分類の周期はそのまま回る
- ページ整形で直した書類は、元の PDF が `/state/originals/{書類ID}.pdf` に残る（90 日で消える）
- 書類の `ai-todo` が `ai-done` か `ai-review` に変わり、タイトル・発行元・文書タイプ・書類日付が入っていることを Web UI で確かめる。ディスク上では `発行元/年/日付_タイトル.pdf` に移っている

## 5. 常駐させる

```bash
docker compose -f compose.example.yaml up -d
```

既定では 5 分（`INTERVAL=300`）ごとに、`page-todo` の書類のページを直してから `ai-todo` の書類を分類する。週に 1 回程度、保存ビュー「確認待ち」（`ai-review`・`page-review`・`ai-failed` のいずれか）を見て直す（設計書 10 章）。

AI が新しく作った発行元・文書タイプの名前が気に入らないときは、削除せずに Paperless の管理画面で名前を変えるか、既存のものに統合する。同じ名前を使う他の書類にもまとめて反映される（[D-006](decisions.md)）。削除すると、その名前を使っていた書類は発行元・文書タイプが空になり、ファイルが `未分類/` に移る。

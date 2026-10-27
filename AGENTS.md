# AGENTS.md

paperless-scan-sorter の作業を始める前に、このファイルを最初に読むこと。人も AI エージェントも
ここから読む（`CLAUDE.md` はこのファイルを読み込むだけ）。

## このプロジェクトは何か

ネットワークスキャナーが NAS に保存した PDF を Paperless-ngx に取り込ませ、Claude Code（`claude -p`）
で内容を判定して、タイトル・発行元・文書タイプ・タグ・書類日付を Paperless の REST API で書き戻す
コンテナ（設計書上の名前は **ai-classifier**）。Paperless 本体には手を入れない。

- 設計の正本: [`docs/design.md`](docs/design.md)
- そう決めた理由: [`docs/decisions.md`](docs/decisions.md)

読む順序: 依頼に関係する `docs/design.md` の章 → そこから参照される `docs/decisions.md` の項目。

## 絶対に守る不変条件

これを破る実装は、動いていても差し戻す。

1. **LLM は JSON を返すだけ。** Paperless の操作はすべてこのリポジトリのコードが行う。
   `claude -p` は空の作業ディレクトリで、書き込み系のツールを許可せずに起動する。PDF を
   読ませるときも Read ツールを処理中の 1 ファイルだけに限る（設計書 7 章）
2. **書類の本文と LLM の出力は信頼できない入力。** 出力は検証ルール（設計書 7 章）を通した
   ものだけを使う。パスに入る値（`title`・`correspondent`）は必ず無害化と長さ制限を通す
3. **状態管理用のタグ**（`inbox` `page-todo` `page-review` `ai-todo` `ai-done` `ai-review`
   `ai-failed`）は Claude に候補として渡さず、出力に含まれていても採用しない
4. **失敗の数え方を混ぜない。** 書類に起因する失敗（JSON が壊れている、検証に通らない）だけを
   数え、3 回で `ai-failed`。利用枠の上限・認証エラー・ネットワーク障害は数えず、その周期を打ち切る
5. **タグは置き換えずにマージする。** PATCH の `tags` は置き換えになるので、既存のタグに足して送る
6. **自動マッチで決まった発行元・文書タイプを上書きしない。** 食い違うときは `ai-review` に回す
7. **PDF を自前で書き換えない。** ページ整形は Paperless の API（`bulk_edit`）に任せ、書き換える
   前に元の PDF を `/state/originals/` へ退避する。1 書類で削除するのは最大 2 ページ

## 禁止事項

- **環境固有の値をリポジトリに含めない。** このリポジトリは公開している。IP アドレス、ホスト名、
  ドメイン、プール名・データセットの実パス、ユーザー名、メールアドレス、トークン、実在の書類の
  内容や取引先名を、コード・テスト・ドキュメント・コミットメッセージに書かない。例には
  `<pool>`、`paperless.example.com`、`○○銀行` のような一般的な名前を使う
- **秘密をログ・例外メッセージ・状態ファイルに出さない。** `PAPERLESS_TOKEN` と
  `CLAUDE_CODE_OAUTH_TOKEN` は環境変数（またはシークレットファイル）で受け取り、イメージに
  焼き込まない。`ANTHROPIC_API_KEY` は前提にしない
- **外部コマンドをシェル文字列で組み立てない。** `claude`・`pdftotext`・`tesseract` などは必ず
  引数配列で起動する
- **設計書にない判断を黙って入れない。** 迷ったら実装を止めて質問する。決めた場合は
  `docs/decisions.md` に追記し、必要なら `docs/design.md` も同じ変更で直す

## コーディング規約

- Python は `>=3.14`。実際に使う版は `.python-version` で固定してある（配るイメージと同じ版で試すため）
- すべてのモジュールは `from __future__ import annotations` で始める
- ruff の設定は `pyproject.toml` にある。`docs/` は ruff の対象外（`extend-exclude`）。ruff は
  Markdown 内のコードブロックも整形するので、外すと設計書そのものが書き換わる
- **コメントと docstring は日本語で書く。**
- **コード内コメントは、いま書かれているコードを現在形で説明する。** 「何をするか / なぜそうして
  いるか」に限り、過去の経緯（「以前は〜だった」「〜へ移行した」）は書かない。経緯は `docs/` と
  コミット本文に残す
- 日時は Paperless とのやり取りでタイムゾーン付きで扱う。タイムゾーンの実値をコードに書かない

## 開発コマンド

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## テストの作法

- 実装より先に**失敗するテストを書き、失敗を確認してから**最小実装する
- 検証ルール・無害化・タグのマージ・失敗の数え方のように、壊れても画面上は正常に見える部分は
  単体テスト必須
- Paperless と `claude` は外部プロセス・外部サービスなので、テストでは差し替える。実物を要する
  テストはマーカーを付けて既定では走らせない

## 変更の進め方

- **1 つの変更 = 1 つの PR = 1 つのブランチ = 1 つの worktree。** worktree はリポジトリ内の
  `.worktrees/<topic>` に作る（`git worktree add -b feat/foo .worktrees/foo origin/main`）。
  `.worktrees/` は追跡しない。マージしたら worktree とブランチを消す
- ブランチ名: `feat/<topic>`、`fix/<topic>`、`docs/<topic>`、`chore/<topic>`、`refactor/<topic>`
  （Issue があれば `feat/<issue#>-<topic>`）
- `main` へは PR で入れ、squash merge する。CI が通っていること
- 振る舞いを変える PR は、同じ PR で `docs/design.md` を直す。設計と実装を食い違わせない

## コミット

Conventional Commits（`type(scope): 件名`）。type は `feat` `fix` `docs` `chore` `refactor`
`test` `perf` のいずれか。件名と本文は日本語で書き、**なぜそうしたか**を本文に残す。判断の理由は
コミット本文と `docs/` にしか残らない。

例: `feat(classifier): 検証に通らない出力を書類起因の失敗として数える`

コミットメッセージ・PR・Issue に、AI との会話のセッション URL を書かない。

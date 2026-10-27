# 開発への参加

paperless-scan-sorter は作者が自宅の NAS で使うために作っている個人プロジェクトです。Issue や PR は歓迎しますが、設計の方針に合わないものは取り込めないことがあります。大きな変更は、先に Issue で相談してください。

## 最初に読むもの

- [AGENTS.md](AGENTS.md): 実装の**不変条件・禁止事項・コーディング規約**。人も AI エージェントもここから読む
- [docs/design.md](docs/design.md): 設計の正本。変更する機能の章を読む
- [docs/decisions.md](docs/decisions.md): 設計判断とその理由。記録された判断を覆すときは、理由を添えて先に相談する

## 開発環境

- [uv](https://docs.astral.sh/uv/)。Python の版は `.python-version` で決まり、uv が用意する

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## 変更の進め方

1. **設計**: 振る舞いが変わる変更は、先に `docs/design.md` を直すか Issue で相談する
2. **テストを先に**: 受け入れ条件をテストにしてから実装する
3. **ドキュメント**: 判断と理由を `docs/decisions.md` に残す
4. **PR**: lint とテストを通す。CI（[.github/workflows/ci.yml](.github/workflows/ci.yml)）が同じものを走らせる

コミットメッセージと PR のタイトルは Conventional Commits（`type(scope): 件名`）で、日本語で書いてください。

## 公開リポジトリであることへの注意

このリポジトリは公開しています。IP アドレス、ホスト名、データセットの実パス、トークン、実在の書類の内容などの環境固有の情報を、コード・テスト・ドキュメント・Issue・PR に含めないでください。ログや設定を貼るときは伏せてください。

## ライセンス

コントリビューションは [Apache License 2.0](LICENSE) で提供されるものとします。

# claude-codex-usage

Claude と Codex のプラン使用率とリセットまでの時間を常時表示する小さなツール。
GUI 版は両方、端末版は Claude のみを表示します。実行した人のローカル環境を参照します。

## 準備

動作確認は **macOS のみ**です。

- Python 3.9 以上。GUI は Tk が必要です（動作確認済み: Python 3.14.7 / Tk 9.0）。
- Claude: Claude Code でサブスクリプションのアカウントにログインしておく。
- Codex: ローカルのセッション記録に使用量が含まれる環境で、一度応答を受け取っておく。

Python の準備には [mise](https://mise.jdx.dev/) を使えます。

## 使い方

```sh
git clone https://github.com/chokoGtr/claude-codex-usage.git
cd claude-codex-usage
mise trust
mise install
mise exec -- python claude_codex_usage_gui.py  # GUI
# mise exec -- python claude_usage.py          # 端末版
```

Python / Tk が揃っていれば `python3 claude_codex_usage_gui.py` でも起動できます。
追加の Python パッケージや、このツールへのトークン入力は不要です。

- `--interval 300`: 更新間隔（秒、既定 300、下限 120）。
- `--topmost`: GUI を常に手前に表示。
- GUI の ↻ または `Command-R` / `Ctrl-R` で手動更新。端末版は `Ctrl+C` で終了。
- 端末版と GUI 版は合わせて 1 つだけ起動できます。

## 注意点

- Claude は Claude Code が保存した認証情報を読み、非公式の使用量 API にアクセスします。公式ツールではなく、仕様変更などで動かなくなる可能性があります。認証エラー時は Claude Code で再ログインしてください。
- Codex は `~/.codex/sessions/`（`CODEX_HOME` 指定時はその配下）の記録を読むだけです。更新ボタンは記録の再読込で、使用量の値自体は Codex が応答を受け取ったときに更新されます。アカウント切替後に古い記録が表示される場合もあります。
- 片方が未設定でも、もう片方の取得は続きます。取得失敗中は最後に取得できた値を表示します。

## テスト

```sh
mise exec -- python -m unittest discover -s tests -t .
```

テストは実際の認証情報・セッション記録を使わず、通信もしません。

MIT License。Anthropic / OpenAI とは関係のない個人プロジェクトです。

# claude-codex-usage

Claude と Codex のプラン使用率とリセットまでの時間を常時表示する小さなツール。
GUI 版は両方、端末版は Claude のみを表示します。実行した人のローカル環境を参照します。

## 準備

動作確認は **macOS** と **Windows の WSL2**（Ubuntu 24.04、GUI は WSLg）です。

- Python 3.9 以上。GUI は Tk が必要です（動作確認済み: Python 3.14.7 / Tk 9.0）。
- Claude: Claude Code でサブスクリプションのアカウントにログインしておく。
- Codex: ローカルのセッション記録に使用量が含まれる環境で、一度応答を受け取っておく。

Python の準備には [mise](https://mise.jdx.dev/) を使えます。

### WSL の場合

- WSL から GUI を起動すると、Windows 側に Python（Tk 付き）があれば **Windows のネイティブウィンドウ**で表示します（ダークモード・高 DPI 対応）。Windows の Python は `pyw` / `pythonw` / `py` / `python` の順に探します。認証情報とセッション記録は WSL 側のもの（`~/.claude`、`~/.codex`）を `\\wsl.localhost` 経由で読みます。
- Windows 側に Python が無い場合や `--wslg` を付けた場合は WSLg で表示します。mise の Python の Tk は日本語を表示できないため、WSLg で使うときは `sudo apt install python3-tk fonts-noto-cjk` を入れて `python3 claude_codex_usage_gui.py --wslg` で起動してください。
- Windows 側にインストールした Claude Code / Codex を参照したい場合は、次のように指定します（`<ユーザー名>` は Windows のユーザー名）。

  ```sh
  export CLAUDE_CONFIG_DIR=/mnt/c/Users/<ユーザー名>/.claude
  export CODEX_HOME=/mnt/c/Users/<ユーザー名>/.codex
  ```

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
- `--wslg`: WSL で Windows の Python を使わず WSLg で表示。
- `--claude [名前=]DIR`: GUI に表示する Claude の設定ディレクトリ。繰り返し指定できます。省略時は自動で探します（下記）。
- GUI の ↻ または `Command-R` / `Ctrl-R` で手動更新。端末版は `Ctrl+C` で終了。
- 端末版と GUI 版は合わせて 1 つだけ起動できます。

### 複数の Claude アカウント

Claude Code はアカウントごとに設定ディレクトリを分けてログインしておきます。

```sh
CLAUDE_CONFIG_DIR="$HOME/.claude-work" claude   # 起動後 /login
```

GUI は `--claude` を省くと、`~/.claude` と `~/.claude-*` のうちログイン済みのものを並べます。名前は `.claude-work` なら `work` です。`CLAUDE_CONFIG_DIR` を指定しているときは、自動では探さずそのディレクトリだけを表示します。

表示するアカウントや名前を決めたいときは `--claude` で指定します。

```sh
python3 claude_codex_usage_gui.py --claude 個人=~/.claude --claude ~/.claude-work
```

このツールはトークンを更新しないため、しばらく Claude Code で使っていないアカウントは期限切れと表示されます。そのアカウントで Claude Code を一度起動すると戻ります。

## 注意点

- Claude は Claude Code が保存した認証情報（macOS は Keychain、それ以外は `~/.claude/.credentials.json`。`CLAUDE_CONFIG_DIR` や `--claude` 指定時はその配下。macOS ではパスから決まる名前の Keychain 項目）を読み、非公式の使用量 API にアクセスします。公式ツールではなく、仕様変更などで動かなくなる可能性があります。認証エラー時は Claude Code で再ログインしてください。
- Codex は `~/.codex/sessions/`（`CODEX_HOME` 指定時はその配下）の記録を読むだけです。更新ボタンは記録の再読込で、使用量の値自体は Codex が応答を受け取ったときに更新されます。アカウント切替後に古い記録が表示される場合もあります。
- 片方が未設定でも、もう片方の取得は続きます。取得失敗中は最後に取得できた値を表示します。

## テスト

```sh
mise exec -- python -m unittest discover -s tests -t .
```

テストは実際の認証情報・セッション記録を使わず、通信もしません。

MIT License。Anthropic / OpenAI とは関係のない個人プロジェクトです。

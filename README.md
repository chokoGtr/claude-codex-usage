# claude-usage

Claude（claude.ai / Claude Code 共通）のプラン使用量を端末に常時表示する。

```
 Claude Usage

 5時間枠          ██████████░░░░░░░░░░░░░░   43%  1h 54m    9/17(木) 18:40
 週次 (全モデル)  ████████░░░░░░░░░░░░░░░░   33%  4d 9h     9/22(火) 02:00
 週次 (Fable)     ████████████░░░░░░░░░░░░   52%  4d 9h     9/22(火) 02:00  ←

 最終更新 16:45:40
```

## 使い方

```sh
mise install                          # 初回のみ
mise exec -- python claude_usage.py       # 端末版
mise exec -- python claude_usage_gui.py   # GUI 版
```

実行時依存はないので、mise が無い環境では `/usr/bin/python3 claude_usage.py` でも動く（Python 3.9 以上）。

| 引数 | 既定 | 説明 |
| --- | --- | --- |
| `--interval` | 300 | ポーリング間隔（秒）。120 未満を指定しても 120 に丸める |
| `--topmost` | off | GUI 版のみ。ウィンドウを常に手前に表示する |

端末版と GUI 版は合わせて1つしか起動できない。2つ目は「既に起動しています」と出して終了コード 1 で終わる。ロックは `$TMPDIR/claude-usage-<uid>.lock` への `flock` で、プロセスが終われば自動で外れる。

取得に失敗すると間隔を倍々に延ばす（通常の間隔を下回らない）。上限は 300 秒で、429 のときだけ 1800 秒。`Retry-After` の方が長ければそちらに従う。

`Ctrl+C` または `SIGTERM` / `SIGHUP` で終了し、代替画面バッファとカーソル表示を必ず元に戻す。

`←` は API が `is_active` を立てている枠。意味は未確認なので印を付けるだけで解釈はしない。
リセット日時はローカル時刻で表示する。端末幅が足りないときは省く。
使用率 80% 以上を黄、95% 以上を赤で表示する。取得に失敗している間は最後に取れた値と最終更新時刻を表示し続ける。

## GUI 版

`claude_usage_gui.py` は `claude_usage` を import し、取得・解釈（`Poller` / `extract_windows` / `fmt_remaining` / `fmt_reset_at` / `backoff_delay`）をそのまま使う。差し替えているのは描画層だけで、この層はトークンに触れない。

取得はワーカースレッドで回し、結果を `queue` 経由で Tk のメインスレッドに渡す。ウィンドウを閉じると `threading.Event` が立ってスレッドの待機が即座に解ける。

背景色は `systemWindowBackgroundColor` の輝度から明暗を判定するため、macOS のダークモードに追従する。使用率の閾値（80% 黄 / 95% 赤）は端末版と同じで、テストで両者の一致を検証している。

Tk 9.0 を前提にしている。`/usr/bin/python3` の Tk 8.5 は Apple の古いビルドなので、GUI 版は mise の Python で動かすこと。端末版は従来どおり Python 3.9 以上であれば動く。

## テスト

```sh
mise exec -- python -m unittest discover -s tests -t .
```

ネットワークと Keychain は mock するため、実トークンも通信も発生しない。

## 認証情報の扱い

Claude Code が保存している OAuth アクセストークンを読み取り専用で使う。

- macOS: Keychain（`security find-generic-password -s "Claude Code-credentials" -w`）
- それ以外: `~/.claude/.credentials.json`

- トークンは起動時に一度だけ読み、`TokenStore` の内部に閉じ込める。値を返す getter は持たない
- 再読み込みは 401 を受けたときと期限切れのときだけ。リフレッシュは実装しない
- 期限切れなら「Claude Code を一度起動してください」と表示して待機する。待機中は API を叩かず、Keychain の再読み込みだけを続けて新しいトークンが入るのを待つ
- 例外は `UsageError` / `CredentialsError` に詰め替え、`raise ... from None` でチェーンを切る。メッセージにトークンもリクエストヘッダも載せない
- 通信先は `api.anthropic.com` のみ

## API

非公式エンドポイント `GET https://api.anthropic.com/api/oauth/usage`。必要なヘッダは `Authorization: Bearer <token>` のみ（実測。`anthropic-beta` / `anthropic-version` / `Accept` はいずれも不要）。

枠の一覧は `limits[]` を正とする。`kind` は `session` / `weekly_all` / `weekly_scoped`（`scope.model.display_name` にモデル名）。`limits` が無い応答では `five_hour` / `seven_day` / `seven_day_*` の固定キーにフォールバックする。未知のキーや未知の `kind` が来ても落ちない。

## 未確認

すべて非公式仕様のため、以下は実物で確認できていない。

- 429 の制限の仕組み。単一プロセスでは 60 秒間隔でも 429 になり、成功はおよそ 4 分に 1 回だった。容量 2 程度のトークンバケットと推定しているが未確認。同じ OAuth トークンを使う Claude Code のトラフィックと枠を共有しているかも未確認（429 は `Retry-After: 0` を返し、`RateLimit-*` ヘッダは無い）
- 5xx / 401 時のレスポンス形式
- `severity` の取りうる値（観測できたのは `normal` のみ）
- `is_active` の意味
- `percent` / `utilization` が 100 を超えうるか
- `locked_reason` の型と値（観測時はすべて null）
- `nimbus_quill` など意味不明なトップレベルキーの用途
- `~/.claude/.credentials.json` の構造（macOS では存在しないため Keychain 版と同形かは未検証）

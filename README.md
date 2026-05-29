# sonsial x view

Twikit + FastAPI + Jinja2 + Tailwind CDN で作る、Twitter/X風のWebビューアです。APIキー不要で、X/TwitterアカウントにTwikitでログインしてタイムライン、検索、ユーザーページを表示します。

## フォルダ構成

```text
sonsial x view 2/
├─ main.py
├─ requirements.txt
├─ render.yaml
├─ .env.example
├─ README.md
├─ templates/
│  ├─ base.html
│  ├─ index.html
│  ├─ search.html
│  ├─ user.html
│  ├─ error.html
│  └─ partials/
│     ├─ sidebar.html
│     └─ tweet_card.html
└─ static/
   ├─ css/
   │  └─ app.css
   └─ js/
      └─ app.js
```

## ローカルセットアップ

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

`.env` を編集します。

```env
TWIKIT_USERNAME=your_x_username
TWIKIT_EMAIL=your_email@example.com
TWIKIT_PASSWORD=your_password
TWIKIT_TOTP_SECRET=
```

起動コマンド:

```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

ブラウザで `http://127.0.0.1:8000` を開きます。

## Cookie保存

- 初回起動時は `.env` のログイン情報でTwikitログインします。
- 成功すると `data/cookies.json` にCookieを保存します。
- 次回起動時はCookieを読み込み、自動ログインします。
- Cookieが期限切れ・失敗した場合は削除して再ログインします。

## キャッシュとrate limit対策

- `CACHE_TTL_SECONDS` でタイムライン/検索/ユーザーのキャッシュ秒数を変更できます。
- `REQUEST_MIN_INTERVAL_SECONDS` でTwikitリクエスト間隔を調整できます。
- `RETRY_ATTEMPTS` と `RETRY_BACKOFF_SECONDS` でエラー時の再試行を調整できます。

## Renderデプロイ

1. このリポジトリをGitHubにpushします。
2. Renderで **New Web Service** を作成し、リポジトリを選びます。
3. `render.yaml` を使う場合、Build Command / Start Command は自動設定されます。
4. RenderのEnvironmentに以下を追加します。

```text
TWIKIT_USERNAME
TWIKIT_EMAIL
TWIKIT_PASSWORD
TWIKIT_TOTP_SECRET（2FAを使う場合のみ）
CACHE_TTL_SECONDS
REQUEST_MIN_INTERVAL_SECONDS
```

`render.yaml` の起動コマンド:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Procfileは不要です。

## 注意

TwikitはX/TwitterのWeb API挙動に依存します。短時間の大量アクセスはアカウント制限につながる可能性があるため、キャッシュ時間とリクエスト間隔は余裕を持って設定してください。

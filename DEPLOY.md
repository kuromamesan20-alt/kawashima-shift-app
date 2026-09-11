# 希望入力アプリを Streamlit Cloud に置く手順

施設の担当者がブラウザで希望休・希望出勤を入力する画面を公開する。
共生の里のアプリと同じ構成。

## 1. Googleスプレッドシートを用意する

1. 新しいスプレッドシートを作る(名前は何でもよい)
2. URL の `/d/` と `/edit` の間がスプレッドシートIDなので控える
   `https://docs.google.com/spreadsheets/d/【ここがID】/edit`

## 2. サービスアカウントを作る

1. Google Cloud Console でプロジェクトを作る
2. 「APIとサービス」→ Google Sheets API と Google Drive API を有効にする
3. 「認証情報」→ サービスアカウントを作成 → 鍵(JSON)を作成してダウンロード
4. **1で作ったスプレッドシートを、そのJSONの `client_email` に編集権限で共有する**
   (これを忘れると保存できない)

## 3. GitHub に上げる

Streamlit Cloud は GitHub のリポジトリを見るので、このフォルダを push する。

```bash
git init
git add .
git commit -m "希望入力アプリ"
git remote add origin <リポジトリのURL>
git push -u origin main
```

`data/` には勤務条件が入るので、公開リポジトリにする場合は扱いに注意する。
**非公開リポジトリを推奨**。

## 4. Streamlit Cloud で公開する

1. https://share.streamlit.io にサインイン
2. 「New app」→ リポジトリと `app.py` を指定
3. 「Advanced settings」→ Secrets に下記を貼る

```toml
requests_sheet_id = "1で控えたスプレッドシートID"
requests_worksheet_name = "requests"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "..."
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```

2で落としたJSONの中身をそのまま写す。`private_key` の改行は `\n` のままでよい。

4. Deploy を押す。URLが出たら施設の担当者に共有する。

## 確認

画面の上に出る「保存先」を見る。

- **Googleスプレッドシート** … 設定できている
- **この端末のファイル** … Secrets が読めていない(貼り忘れ・書式ずれ)

## 手元で動かす場合

```bash
./venv/bin/python -m streamlit run app.py
```

Secrets が無いので `data/staff_profiles/YYYY-MM_希望.yaml` に保存される。
画面の見た目を確かめるにはこれで十分。

## 入力された希望を使って勤務表を組む

スプレッドシートから読む機能は未接続なので、いまは手元のYAMLを使う。

```bash
./venv/bin/python cli.py generate \
  --profiles data/staff_profiles/case001.yaml \
  --requests data/staff_profiles/2026-10_希望.yaml \
  --year 2026 --month 10 \
  --out "data/output/勤務計画表_2026年10月_2病棟全体.xlsx"
```

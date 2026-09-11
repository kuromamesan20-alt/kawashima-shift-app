# 勤務表作成ツール（案件001）

## 登場人物

| | 役割 |
|---|---|
| **稲葉** | このツールの作り手。勤務表の作成を代行する |
| **施設の担当者** | お客様。施設側の担当者。スタッフ情報と希望休・希望出勤を提供する |
| スタッフ34名 | 実際に勤務する人たち |

施設の担当者から届くスタッフ条件CSVを読み取り、月次勤務表のたたき台を作る。

## 考え方

条件欄が自由記述で表記揺れも誤字もあるため、**自動抽出だけでは確定させない**。

```
CSV → 自動抽出(ドラフトYAML) → 人が確認・修正 → 勤務表生成 → 自動検証
                                    ↑ここが要
```

自動で読み取れなかった文は捨てずに `unparsed_notes` に残し、`review_reasons` に
「なぜ人が見る必要があるか」を書き出す。特に**同席制約は絶対厳守のハード制約**なので、
抽出に成功していても必ず人の確認に回す。

## セットアップ

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 使い方

### 1. CSVから条件を構造化する

```bash
./venv/bin/python cli.py extract-profiles \
  --input "data/input/スタッフカード_案件001_回答 - スタッフ情報.csv" \
  --out data/staff_profiles/case001_draft.yaml
```

要確認のスタッフとその理由が一覧で表示される。

### 2. 読み取り結果を眺める(HTML)

```bash
./venv/bin/python cli.py review-list \
  --profiles data/staff_profiles/case001_draft.yaml \
  --out "data/output/案件001_読み取り結果.html"
```

「顧客が書いた原文」と「読み取った内容」を左右に並べた一覧。要確認だけに絞り込める。

### 3. お客様に確認していただく

読み取れなかった点・解釈が正しいか自信がない点を、**お客様(施設)にそのまま聞ける質問**にして書き出す。

```bash
./venv/bin/python cli.py export-questions \
  --profiles data/staff_profiles/case001_draft.yaml \
  --out "data/output/案件001_お客様確認シート.xlsx" \
  --case-name "案件001"
```

Googleスプレッドシートにアップロードして共有し、黄色い「ご回答」欄に記入していただく。
「整理番号」列(非表示)は回答を元に戻すために使うので、**消さないようお伝えする**。

記入されたものを取り込む(xlsx でも CSV でも可)。

```bash
./venv/bin/python cli.py import-answers \
  --profiles data/staff_profiles/case001_draft.yaml \
  --answers "data/output/案件001_お客様確認シート_回答済み.xlsx" \
  --out data/staff_profiles/case001_回答反映.yaml
```

- 「はい（合っています）」と**選択式で答えられた項目だけ**が確認済みになる
- 自由記入の回答は**記録するだけで自動反映しない**。内容を見て、次の手順のExcelで直す
- 整理番号はスタッフごとに固有なので、同じシートを2回取り込んでも別の項目を誤って消さない

### 4. Excelで確認・修正する

```bash
./venv/bin/python cli.py export-review \
  --profiles data/staff_profiles/case001_draft.yaml \
  --out "data/output/案件001_勤務条件_確認用.xlsx"
```

- **灰色の列は読み取り専用**。白い列を直す
- **ID列は絶対に書き換えない**(本人の特定に使う。名前を書き換えても、IDが正しければ正しい人に反映される)
- 書き方は「書き方の凡例」シートを見る
- 直した行の「**確認済み**」を「はい」にする ← これが確認完了の印

### 5. 修正をYAMLに反映する

```bash
./venv/bin/python cli.py import-review \
  --profiles data/staff_profiles/case001_draft.yaml \
  --review "data/output/案件001_勤務条件_確認用.xlsx" \
  --out data/staff_profiles/case001.yaml
```

読めない記入があれば、**どの行のどの列がなぜ読めなかったか**を表示する。その項目だけ反映されない
(同じ行の他の変更は保存される)ので、Excelを直してもう一度実行する。

`case001.yaml` が以後の**正のデータ**になる。

### 6. その月の希望休・希望出勤を集める

勤務表は「事前に聞き取った希望」を前提に組む。毎月このシートを作って施設の担当者に渡す。

```bash
./venv/bin/python cli.py export-requests \
  --profiles data/staff_profiles/case001.yaml \
  --year 2026 --month 10 \
  --out "data/output/2026年10月_希望休・希望出勤.xlsx"
```

- 31名×その月の日付のマス目。セルはプルダウンで `公`(希望休) / `日①`〜`日⑨` / `日` / `○` / `◉` を選ぶ
- **記入済みのシートは上書きできない**(`--force` を明示しない限りエラーになる)。施設の担当者の記入を消す事故を防ぐため
- `--carry-over` に前月のシートを渡すと、記入内容を引き継げる。その月に無い日付の記入は破棄され、件数が報告される

記入されたものを読み込む。

```bash
./venv/bin/python cli.py import-requests \
  --requests "data/output/2026年10月_希望休・希望出勤.xlsx" \
  --profiles data/staff_profiles/case001.yaml \
  --out data/staff_profiles/2026-10_希望.yaml
```

`--profiles` を渡すと、行が丸ごと消えているスタッフを警告する。

### 7. 勤務表を組んでExcelに出す

```bash
./venv/bin/python cli.py generate \
  --profiles data/staff_profiles/case001.yaml \
  --requests data/staff_profiles/2026-10_希望.yaml \
  --year 2026 --month 10 \
  --out "data/output/勤務計画表_2026年10月_2病棟全体.xlsx"
```

実物と同じ体裁で出力する。ユニット区切り、1人2行(2行目は中抜けと責任者「せ」)、
右端の集計、最下部の日別チェック。組めない場合は理由を表示する。

### 8. 希望入力のWebアプリ

施設の担当者がブラウザで希望を入力する画面。

```bash
./venv/bin/python -m streamlit run app.py
```

Streamlit Cloud への公開手順は `DEPLOY.md` を見る。

## テスト

```bash
./venv/bin/python -m pytest tests/ -q
```

パーサーを直すときは、**先に `tests/test_condition_parser.py` に実データの文言でケースを足してから**直すこと。

## ディレクトリ

| パス | 中身 |
|---|---|
| `data/input/` | 顧客から届くCSVを置く場所 |
| `data/staff_profiles/` | 構造化した勤務条件(ドラフト / 確認済み) |
| `data/output/` | 生成した勤務表・検証レポート |
| `config/shift_rules.yaml` | シフト区分の時間帯定義。施設に合わせて要調整 |
| `src/kawashima_schedule/` | 本体 |

## 現状と次の作業

- [x] CSV読み込み(テスト送信行の除外、列順変更に強いマッピング)
- [x] 自由記述の条件パーサー + 回帰テスト
- [x] 構造化データのYAML入出力
- [x] 読み取り結果の一覧(HTML)
- [x] Excelでの確認・修正と読み戻し
- [x] お客様への確認シートと回答の取り込み
- [x] 月ごとの希望休・希望出勤シート
- [x] OR-Tools CP-SATによる勤務表生成
- [x] 実物と同じ体裁のExcel出力
- [x] 希望入力のWebアプリ(Streamlit)
- [ ] Webアプリに入力された希望を勤務表生成につなぐ
- [ ] 検証レポート

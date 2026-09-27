# 株主総会 会場マップ

上場会社(東証プライム・スタンダード・グロースなど。TOKYO PRO Market は除外)の株主総会の日時と会場を地図で表示するサイトです。
GitHub Actions が毎週月曜の朝7時(日本時間)にデータを更新し、GitHub Pages に自動で反映します。

## フォルダ構成

| 場所 | 中身 |
|---|---|
| `docs/index.html` | 地図のページ(GitHub Pages で公開される) |
| `docs/agm.geojson` | 地図に表示するデータ(毎週自動で更新) |
| `scripts/tdnet_agm_scraper.py` | TDnet から招集通知などを取得し、日時・会場を抽出 |
| `scripts/merge_agm.py` | 毎週の結果をこれまでのデータに統合 |
| `scripts/geocode_agm.py` | 住所を緯度経度に変換(国土地理院API) |
| `data/agm_all.csv` | これまでに集めた全データ |
| `data/agm_failed.csv` | 位置を特定できなかった総会(バーチャルオンリー総会など) |
| `.github/workflows/weekly-update.yml` | 週1回の自動更新の設定 |

## 初回のセットアップ

1. GitHub で新しいリポジトリを **Public** で作る(無料で GitHub Pages を使うため)
2. このフォルダの中身をすべてアップロードする(`.github` フォルダも忘れずに)
3. Settings → Pages で、Source を「Deploy from a branch」、Branch を「main」「/docs」にして Save
4. Settings → Actions → General の「Workflow permissions」を「Read and write permissions」にして Save
5. Actions タブ →「週1回のデータ更新」→「Run workflow」で1回目を手動実行する

数分〜数十分で終わり、`https://ユーザー名.github.io/リポジトリ名/` に地図が表示されます。
以降は毎週月曜の朝に自動で更新されます。

## 注意

- TDnet の公開ページは開示から31日分しか残らないため、週1回の実行でデータを貯めていく仕組みです。
- 抽出は自動なので誤りが含まれる可能性があります。ページ上でも各社の招集通知で確認するよう案内しています。
- TDnet は robots.txt で自動アクセスを制限しています。サーバーに負担をかけないようアクセス間隔を空けていますが、運用はご自身の判断で行ってください。

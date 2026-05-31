株式アノマリー候補抽出 v1.1 GitHub Pages / Actions 手順

■アップロードするもの
ZIPを解凍し、中身をすべてGitHubリポジトリにアップロードする。
index.htmlだけでは動きません。以下が必要です。

・index.html
・manifest.webmanifest
・sw.js
・README_iPhone_API.txt
・README_iPhone_GitHubActions.txt
・data/candidates.json
・data/update_log.json
・scripts/screen_stocks.py
・.github/workflows/update-candidates.yml

■GitHub Pages設定
Settings → Pages
Source：Deploy from a branch
Branch：main
Folder：/root
Save

■GitHub Actions実行
Actions → Update stock anomaly candidates → Run workflow

■自動更新
平日 22:30 UTC に実行する設定です。
日本時間では翌朝 7:30 頃です。
cronを変更すれば更新時刻は調整できます。

■Stooq自動取得が失敗する場合
Stooq一括ダウンロードが認証で拒否される場合があります。
その場合はStooqから以下を手動でダウンロードし、data/raw/ に置いてからActionsを実行してください。

・d_us_txt.zip
・d_jp_txt.zip

■注意
このアプリはスクリーニング用です。
売買推奨、投資助言、利益保証ではありません。
CAN SLIMのC/A/Iは日足データだけでは確認できないため、決算情報で別途確認してください。

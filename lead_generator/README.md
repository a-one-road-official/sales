# 非AIリスト収集・追記ワーカー

## 現在できること／未完了

- 公開 RoboticsTomorrow 企業ディレクトリからプロフィールを決定論的に収集する。
- SQLiteに候補、証拠付きレコード、両リスト書込状態、エラー、操作履歴を保存する。
- 証拠JSONを検査し、PASSだけを既存SSOT・生贄タブに追記する。
- 既存企業を会社名・公式ルートドメインで保守的に照合し、既存Statusを変更しない。
- 書込結果が不明なら再読し、片側成功は再開時に認識する。未確定のまま自動再追記しない。
- 500社時点で同じ新規会社の存在を両リストで再確認し、目標2,000社まで進む。
- 既存Sales Control!B46でSTART/STOP。UIも同じローカル状態を操作可能。

**未完了：全候補の公式サイト・展示会・資金・日本拠点・LinkedIn根拠を自動収集して証拠JSONに変換する処理。**
現在のディレクトリ収集だけでは適格会社は増えない。ディレクトリ掲載は出展証拠にしない。
入力の意味が正しいかは証拠作成側の責任であり、この検査器は証拠文の意味を理解するモデルではない。
既存のGoogleサービスアカウントによる両リスト認証を実地確認済み。
GitHubの`Non-AI lead intake`ワークフローをmainへ配備すると20分ごとの有界処理になる。
**自動収集は公開プロフィールの事実収集まで。合否の全根拠を自動完成する機能は未完成。**
条件確認済みレコードだけが追記対象。これを2,000社の選定・追加を保証する完成品と扱わない。

## 無料の定期実行

- publicリポジトリの標準ubuntu-latestのみ。privateでの実行は拒否する。
- 有料モデルAPI、Cloud Runデプロイ、従量ストレージ、顧客向け送信を使わない。
- 既存Sales Control!B46のSTART/STOPが操作元。停止要求は次のプロフィール取得・追記前に確認。
- 私有の処理状態を同じSales Controlの予約領域I64:P200へ圧縮保存する。新しいタブは作らない。
- 2つの保存領域を交互に使い、検査後に参照先を切り替える。中断時も直前の記録から再開する。
- 公開Actionsログには会社情報・リスト内容を出さず、ArtifactやCacheにも保存しない。
- robots.txtを尊重。アクセス拒否・認証・チャレンジは停止し、迂回しない。通信エラーは最大3回の周期再試行。
- 予約領域に別のデータがあれば停止。予約領域は手動で編集しない。
- 詳細取得済み件数はB58。収集件数B51と条件確認済みB52・両側追加B53は区別する。

## 初回案件に適用する方針

- 初回は顧客開拓・有効リード・顧客または販売パートナー商談。日本語資料調整は必要範囲。
- 物流最適化、工場画像検査、RFID/資産追跡、予知保全、製造データ、OTセキュリティを優先。
- 全日FDE・共同論文が初回提供の必須条件なら不適合。
- 台湾、韓国、インド、イスラエル、小国欧州を優先。研究材料は低優先で保持。
- 売上非公開だけでは落とさない。公表された財務/予算証拠、または商用導入＋複数展示会を支払い余力の兆候とする。
- 支払い能力の兆候と前払いへの同意は別の項目。後者は商談まで未確認。
- 日本直接拠点/カントリーマネージャーありは除外。代理店のみは許容。
- 取得失敗や曖昧な日本拠点結果はREVIEW。検索範囲内で見つからない旨を記録する。

## 起動（持続ディスクのある許可済みホスト）

Python 3.11以上。モデル・Vertex・Cloud Run・送信ワーカーは呼び出さない。

```bash
python -m pip install -r lead_generator/requirements.txt
python -m lead_generator.cli --db /var/lib/aone-leads/state.sqlite discover
python -m lead_generator.cli --db /var/lib/aone-leads/state.sqlite import-evidence /private/evidence.jsonl
python -m lead_generator.cli --db /var/lib/aone-leads/state.sqlite status
python -m lead_generator.cli --db /var/lib/aone-leads/state.sqlite work --config /private/leadgen.json
```

config.example.jsonを私有領域へコピーし、既存の正しいIDを設定する。Google ADC認証が必要。
認証ファイル、DB、実メール・専門家情報、根拠JSONを公開リポジトリへ置かない。
run_idは再起動でも固定する。DB消失時は同じrun_idで両ブックを再照合する。
同一DB/排他ロックを共有するワーカー1個だけを動かす。別の既存リスト追記ワーカーとの排他調整も必要。
実行前にGoogle Sheets総セル数・両タブの空き容量を確認する。

未精査候補をCRMへ混入させない。PASS入力が尽きるとWAITING_FOR_EVIDENCEとして終了。
常駐ホストのタイマーから `work` を起動する設計だが、この変更ではタイマーはインストールしない。
停止は次の書込直前に確認。既に送信済みのAPI要求は取り消せない。
Google操作欄を使う場合はB46が再起動時の要求元。緊急停止はB46をSTOPへ。

ローカル操作画面：環境変数LEADGEN_UI_PASSWORDを20文字以上の独自パスワードに設定してから
`python -m lead_generator.dashboard --db /var/lib/aone-leads/state.sqlite`。
127.0.0.1:8765、ユーザー名operator。画面は操作要求の記録のみ。実ワーカーと同じ持続DBを使う。

## 確認

```bash
python -m unittest discover -s tests/lead_generator -v
```

タイムアウト後の二重追記防止、片側追記の再開、STOP、既存Status保護、スキーマ変更、
日本担当者あり/不明、代理店のみ、資金情報の代替指標、初回提供範囲をテストする。
実Google認証・追記の成功をこのテストから推定しない。

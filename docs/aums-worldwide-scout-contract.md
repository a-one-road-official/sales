# AUMS Worldwide Scout — Work実行契約

## Mission

A-one roadのAUMS構築と2026年10月31日までの着金を同一プロセスで実行する。米国・中国・日本を本社所在地とする企業を除き、世界から新規海外企業1,000社を発見し、既存SSOTの `営業リスト＿Factory/BPO` へ追加する。

既存行のステータス、履歴、商談情報は変更しない。新規企業の追加だけを行う。会社名・公式URL・本社国・Capability根拠を確認し、重複を排除する。

## G1–G6 Selection Contract

| Gate | 判定内容 | 収録への効き方 | 成果物に残す値 |
|---|---|---|---|
| G1 | 本社国が米国・中国・日本以外 | 必須 | HQ Country / Evidence URL / Checked At |
| G2 | AUMS Capability条件のどれか1つ以上に該当 | 必須、OR判定 | Capability ID / Hit Terms / Technical Evidence |
| G3 | 顧客・導入・受注・売上等のCommercial Proof | 順位付けのみ | Evidence / Unknown |
| G4 | 従業員・資金調達・売上等の支払能力Signal | 順位付けのみ | Evidence / Unknown |
| G5 | Funding、工場増設、海外展開、展示会等のWhy Now | 順位付けのみ | Trigger / Date / Evidence / Unknown |
| G6 | 日本法人・代理店・販売経路・Partner募集 | 順位付けとWS6接続 | Rights Status / Evidence / Inference / Unknown |

最終収録式：`G1 PASS AND G2 PASS`。

G2はAND判定を禁止する。提示条件、Capability Dictionary、検索語のどれか1つに一致し、公式情報または信頼できる企業プロフィールで技術内容を確認できた会社を収録する。G3〜G6の未確認、FAIL、Unknownは除外理由に使わない。

### G2 OR対象

1. Inspection / Metrology / NDT
2. Heavy / Unstructured Material Handling
3. Metal AM / WAAM / Repair
4. Advanced Materials / Powder / Wire / Feedstock
5. Cross-vendor Industrial Data / MES / MOM / IIoT
6. Robot / Process Orchestration / Fleet / Scheduling / Simulation
7. Adaptive Welding / Joining / Surface / Post-processing
8. Design / CAD-CAM Automation / Manufacturing Simulation
9. Manipulation / Identification / Traceability
10. Maintenance / Repair / Retrofit
11. Industrial Automation / Robotics / Machine Tools / Manufacturing Software

## 共通IDと接続規則

- `Program_ID`：政策・公的Program
- `Project_ID`：採択案件
- `Capability_ID`：工程Capability
- `Company_ID`：海外企業・国内企業
- `Buyer_ID`：日本BuyerのSite / Factory単位
- `Rights_ID`：Commercial Rights調査
- `Action_ID`：営業Next Action

すべての成果物は上記IDで接続する。最終的に1行で次を追跡できること。

`Program_ID → Project_ID → Process → Capability_ID → Japan Gap → Company_ID → Buyer_ID → Rights_ID → Paid Validation → Action_ID`

## 成果物の保存先

| 成果物 | 保存先 | 主キー |
|---|---|---|
| 新規海外企業1,000社 | 既存SSOT `営業リスト＿Factory/BPO` | Company_ID / Official Domain |
| 実行状況・件数 | `Sales Control` | Run ID |
| WS1〜WS10の各Master | AUMS Research Workbookの対応Tab | 各共通ID |
| Top 20 Evidence Pack | 企業別1ページ資料 | Company_ID |
| AUMS Architecture v0 | Architecture成果物 | Capability_ID / Company_ID |
| Coverage Audit | Audit Tab | Workstream / Record ID |

SSOTへは新規企業行だけを追加する。WS1〜WS10の分析列・中間成果物をSSOTへ混在させない。

## WS1｜日本造船・製造政策 Demand Map

成果物：Government Program Master、採択案件Master、参加企業・大学・研究機関Master、政策→予算→事業→企業→対象工程の関係、政策要求Capability一覧。

必須列：`Program_ID / Program / Year / Budget / Project_ID / Project / Lead / Partners / Target Process / Target Problem / Technology / Expected Output / Schedule / Capability_ID / Source URL / Retrieved At`。

Definition of Done：公式発表掲載案件100%、一次情報URL100%、AUMS Process Stage分類100%、未分類0、根拠なし推測0。本文と別紙の件数差は原典追跡後にResolvedまたはUnresolvedを記録する。

## WS2｜AUMS Process / Capability Taxonomy

成果物：AUMS Capability Dictionary v1。

工程：Requirements、Design、Simulation、Material、Powder / Wire / Feedstock、Forming / AM、Cutting / Machining、Joining / Welding、Post-processing、Surface Treatment、Inspection / Metrology / NDT、Identification / Traceability、Manipulation、Material Handling / Intralogistics、Assembly、Final QA、Production Planning、Industrial Data、Robot Orchestration、Maintenance / Repair。

必須列：`Capability_ID / Parent Process / English Search Name / Function / Input / Output / Required Performance / Applicable Industries / Existing Japanese Capability / Foreign Capability / Evidence / Maturity`。

Definition of Done：WS1全案件のマッピング100%、原則「その他」0、同義語統合済み、防衛・航空宇宙・重工へ転用可能な抽象度、英語検索名100%。

## WS3｜採択案件の競合・協業・空白分析

成果物：全採択案件を `COMPETE / COOPERATE / SUPPLY / INTEGRATE / LEARN / IRRELEVANT` に分類したMaster。

各案件の必須回答：解決対象、課題Owner、技術スタック、AUMS重複、AUMS不足、相手不足、協業可能性、A-one road介在価値、海外Deeptech挿入点、AUMS Implication。

Definition of Done：対象案件分類100%、競合Layer明示100%、協業接続点明示100%、案件ごとのImplication最低1件、曖昧評価0。

## WS4｜Capability Gap Map

成果物：全Capabilityを `GREEN / YELLOW / RED / BLACK` で判定したGap Map。

必須列：`Capability_ID / Gap Color / Evidence of Demand / Existing Suppliers / Government Signal / Adopted Projects / Technical Bottleneck / Deployment Bottleneck / Commercial Bottleneck / Qualification Barrier / Foreign Technology Opportunity / Source URLs`。

Definition of Done：全Capability評価、RED/YELLOWは需要根拠最低1件、Technology GapとDeployment Gapを別列化、国内企業が十分解決するものはGREEN、情報不足はBLACK。

## WS5｜海外Deeptech Scout

目的：WS4のRED/YELLOWを中心に、G2のどれか1条件へ一致する海外企業を広く収録する。

探索経路：展示会、特許、論文、VC Portfolio、EU/CORDIS等研究Project、業界団体、OEM/Robot/AM Ecosystem、Funding News、Distributor/Integrator Network、製造企業Directory。

成果物：Foreign Capability Candidate MasterとSSOT新規追加1,000社。

必須列：`Company_ID / Company / Country / Official URL / Official Domain / Capability_ID / Product / Hit Terms / Technical Evidence / Commercial Evidence / Customer Evidence / Industries / Funding / Japan Office / Japan Distributor / APAC Presence / Partner Program / Distributor Program / Integration Possibility / AUMS Fit / Japan Demand Match / Source URLs / Retrieved At / G1 / G2 / G3 / G4 / G5 / G6`。

Definition of Done：

- 米国・中国・日本を本社所在地とする企業0社。
- G2は1条件以上のOR一致。
- 公式サイト確認率100%。
- 会社名↔公式URL一致100%。
- Company NameとOfficial Domainの重複排除100%。
- 根拠が取れない補助項目はUnknown。LLM推測補完0。
- 各RED Capabilityは原則10社以上、各YELLOW Capabilityは原則5社以上を探索。
- 市場が薄い場合は検索経路、検索式、確認件数を保存。
- `Discovered`、`Profile Checked`、`G1+G2 Qualified`、`Duplicate Removed`、`SSOT Appended`、`Read-back Verified` を別件数で表示。
- 完了件数は既存行を含まない `SSOT Appended AND Read-back Verified = 1,000`。

## WS6｜Commercial Rights / Agency Opportunity

成果物：WS5企業の `Distributor / Reseller / Representative / System Integrator / VAR / Referral / OEM / White-label / Technology Partner / Direct-sales only / Unknown` 分類。

必須調査：既存日本代理店、APAC Distributor、既存契約地域、日本進出歴、Partner募集、Distributor募集、海外売上、日本顧客、日本語対応、日本法人、Decision Makerまたは担当部署。

Definition of Done：Top候補全社のCommercial Route確認、既存日本代理店明示、Rights StatusをEvidence / Inference / Unknownに分離、推測だけの「代理店取得可能」判定0。

## WS7｜Japanese Buyer / Demand Account Map

成果物：Capability別の日本Buyer Map。対象は造船所、重工、防衛Prime、Tier1/2、航空宇宙、修繕、港湾、大型製造、研究機関等。

必須列：`Buyer_ID / Company / Site / Factory / Relevant Program / Relevant Process / Capability_ID / Observed Problem / Current Supplier / Investment Signal / PoC Possibility / Procurement Route / Evidence`。

Definition of Done：Top CapabilityすべてにBuyer候補、工場・事業・工程レベルまで特定、設備投資・採択・研究・求人・技術資料等で需要を裏付ける。

## WS8｜AUMS × 10月着金 Priority Matrix

成果物：全海外企業をPriority A/B/Cに分類し、既存商談と新規候補を同じMatrixで比較する。

必須列：`Company_ID / P(Oct Meeting) / P(Proposal) / P(Close) / Expected Contract Value / Expected Upfront Cash / AUMS Capability Value / Japan Demand Evidence / Agency Rights Value / Sales Cycle Risk / Technical Integration Value / Priority / Next Action`。

Definition of Done：Top 20抽出、各社の次の1アクション、10月Expected Upfront Cash合計、$100K前受Win定義とのGapを表示。

## WS9｜営業にそのまま使えるEvidence Pack

成果物：Top 20各社について1ページ相当のEvidence Pack。

構成：Why Japan Now、日本の具体的政策・設備投資、対象工程、日本側Capability Gap、Potential Buyers、競合、技術Fit、A-one road介在価値、Japan Validation案、Commercial Structure案、根拠URL。

Definition of Done：企業別に内容を変更、Japan-side evidence最低3件、商談で画面共有できる品質、数字の出典100%、出典不明数字0。

## WS10｜AUMS Architecture v0

成果物：`Japanese Demand → Required Capability → Existing Japanese Capability → Capability Gap → Foreign Deeptech → Commercial Rights → PoC / Deployment → Capability Evidence → AUMS Capability Library` を実データで統合する。

併せて `Product Requirement → Required Processes → Available Capabilities → Missing Capabilities → Manufacturing Route` を表現する。

Definition of Done：最低1つの造船Use CaseでEnd-to-End Routeを作成、各工程へ実在企業・Capabilityを割当、不足工程はMissing、自社開発と外部取得を分離。

## WS11｜最終成果物

A. Executive Summary  
B. Japan Manufacturing Demand Map  
C. AUMS Capability Dictionary  
D. Government / Project Master  
E. Competitor & Collaborator Map  
F. Capability Gap Map  
G. Foreign Deeptech Candidate Master  
H. Agency / Commercial Rights Map  
I. Japanese Buyer Map  
J. October Cash Priority Matrix  
K. Top 20 Evidence Packs  
L. AUMS Architecture v0  
M. Immediate Action List

Immediate Action Listは `TODAY / NEXT 72 HOURS / BY SEP 30 / BY OCT 10 / BY OCT 31` に分ける。

## Global Definition of Done

1. 政策→案件→工程→Capability→Gap→海外企業→日本Buyer→Commercial Rights→営業ActionがIDで接続されている。
2. 全重要主張に一次情報または信頼できる根拠がある。
3. Company↔Official URLの誤紐付け0、重複0、Unknownの推測補完0。
4. 既存日本代理店を確認し、政策予算を市場規模として扱っていない。
5. Technology GapとImplementation / Deployment Gapを区別している。
6. Top 20とNext Action、Expected Upfront Cash、$100K Gapが存在する。
7. 各成果物でCompany ID / Capability ID / Project IDを共通化している。
8. Source URLと取得日を保存している。
9. 最終監査後の件数を報告し、Coverage Auditで未調査・Unknown・Evidence不足を一覧化している。
10. Webで確認可能な「追加調査」は実行し切っている。
11. 新規海外企業1,000社が既存SSOTに追加され、SSOT読戻しで同一会社名・公式Domain・Run Markerが確認されている。

最終報告冒頭の必須数値：政策/Program数、採択Project数、Capability数、RED Gap数、YELLOW Gap数、海外企業調査数、G1+G2適格数、重複除外数、SSOT追加数、読戻し確認数、日本代理店なし確認企業数、日本Buyer数、Top A案件数、10月Expected Upfront Cash、Evidence Coverage %、Unknown残件数。

## 実行停止条件

- `Sales Control!B46 = STOP`
- 公式サイトと企業名の一致が確認できない
- HQ Countryが確認できない
- 米国・中国・日本本社
- G2 Capability一致が0
- SSOT Schema変更、書込先不一致、曖昧なAppend結果
- 同名または同一Official Domainの既存行

上記以外のG3〜G6不足は停止条件にしない。Unknownのまま収録し、WS6・WS8・WS9で追加調査する。

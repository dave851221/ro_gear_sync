# CLAUDE.md — RO_GearSync

公會工具：從雷電模擬器裡的「RO 仙境傳說：世界之旅」擷取 **裝備評分**（本地 OCR）與
**公會聯賽戰績**（Gemini 視覺 API），寫入 Excel。發佈對象是不懂技術的公會幹部
（單一 exe＋config.ini）。本檔取代已刪除的 PLAN.md / PLAN_LEAGUE.md。

## 常用指令

```powershell
.venv\Scripts\python.exe scripts\run_gui.py          # 開發模式跑 GUI
.venv\Scripts\python.exe scripts\league_scan.py      # 聯賽 CLI（互動式五畫面）
.venv\Scripts\python.exe scripts\bootstrap_excel.py  # 裝評 Excel 工具（--update --dry-run）
.venv\Scripts\python.exe scripts\roster_sync.py      # 雲端名冊同步 CLI（--dry-run）
powershell scripts\build_exe.ps1                     # PyInstaller 打包
```

venv 在專案內 `.venv/`（使用者偏好 per-project）。無 pytest——驗證靠手動 smoke test。
本機在 sandbox shell 跑 Tk 需設 `TCL_LIBRARY`/`TK_LIBRARY`（使用者正常啟動不用）。

## 架構地圖（src/ro_gearsync/）

- `adb/` — AdbClient（subprocess 包 adb.exe）、ldconsole 實例列舉、port 掃描
  （LDPlayer 實例 N 的 adb port = 5555+2N；`find_ldplayer_instances` 是 GUI/CLI
  共用的偵測入口，回報 ONLINE/ADB_OFF/NOT_RUNNING/OFFLINE）
- `vision/` — 裝評本地 OCR：RapidOCR 包裝（**cls 永遠關閉**）、版面比例、列解析
- `matching/matcher.py` — 暱稱比對（兩邊共用）：NFKC＋去飾符＋casefold＋OpenCC t2s
  正規化；裝評用 exact correct → exact ocr → fuzzy(ratio/partial/token_set, 門檻65)；
  **聯賽只用 exact**（fuzzy 2026-07-07 廢除：真實戰役中兩個相似名字被交叉配對，
  錯得比不配還糟；非精確命中一律進人工指認，指認後回寫 Last_OCR_ID 下週即精確命中）
- `matching/ocr_variants.py` — 多值 Last_OCR_ID 共用工具（2026-07-10 自
  league/roster.py 抽出，裝評與聯賽自此同格式）：OCR_SEP=｜、上限 8、
  split/merge/primary helpers；**Matcher 一律經 `build_matcher()` 建**——它把
  變體拆好餵 `ocr_aliases` 並鎖 `use_latest_ocr_field=False`（原始「A｜B｜C」
  整串不入池——正規化後會黏成垃圾 key）；建構點共五處（excel.merge_capture、
  scan_runner ×2、app._reocr_worker、league/merge），別再直接 `Matcher(records)`
- `capture/session.py` — 裝評 producer/consumer 擷取管線。**取消不變式**
  （2026-07-10 修死鎖）：producer 每條退出路徑都必須 put `_StopMarker`——
  GUI 中止是第三方 set stop_event，consumer 卡在 queue.get() 只認 marker；
  consumer loop-top 也檢查 stop_event（取消時跳過積壓頁不再 OCR）；
  ScanRunner 取消時發空 summary 短路 fuzzy 階段 → GUI 收到後刪 session 資料夾
- `storage/excel.py` — 裝評 wide-table 工作簿（v2 schema）
- `league/` — 聯賽：model / recognizer(Gemini) / session(拍攝+辨識) / merge(比對+對帳)
  / roster(名冊+回寫) / storage(快照 Excel)
- `roster_sync/` — 雲端名冊同步（2026-07-08）：google_sheets(OAuth 桌面流程
  loopback＋PKCE **手刻**，只靠 stdlib＋requests、不引入 google-auth；token 存
  data/google_token.json) / sheet_parse(表頭列掃描＋編號限 1..150＋收滿 150 或遇
  第二張表頭/「排隊名單」即停——表單下方有編號重新從 1 起算的排隊表；重複編號
  整筆排除並警告；「裝備評分」欄**選讀**（缺欄只警告不擋）、容忍千分位) /
  diff(join/leave/changed/prof 分類；changed 一律人工三選一：換人/改名/略過；
  另產 peak_updates（2026-07-10）——表單裝評 > guild_scores 最高裝評且**名字精確
  一致**才列入，只升不降；GUI 整批一個勾選框、CLI 一次 y/n；反向（本地最高裝評 >
  表單裝評，含表單 cell 空白）只計數進 sheet_stale_peaks——工具**不寫表單**，
  GUI 確認視窗底部/無變更訊息框/CLI 都以紅字提醒使用者手動回填雲端) /
  apply(就地改 cell＋先備份，**不可走** GuildScoresWorkbook.save()
  整本重建；換人與退會清 遊戲ID/職業/Last_OCR_ID＋裝評歷史欄，改名保留歷史只清
  Last_OCR_ID；join/replace 以表單裝評直接填最高裝評、rename 只升不降；
  peak 寫入前有 staleness guard（該列名字變了就略過）；最高裝評就地寫入可持久——
  excel.py 載入端本來就取 max(peak cell, 每日欄)；
  坑：openpyxl `ws.cell(value=None)` 是 no-op，清空必須 `.value = None`)。
  CLI：`scripts\roster_sync.py`（--dry-run / --forget-auth）；GUI 入口在工具選單
- `gui/` — customtkinter。`app.py` 主視窗（CTkTabview：裝備評分/聯賽評分 分頁，
  環境檢查與狀態列共用）；league_panel / league_runner / league_review_dialog；
  裝評的 scan_runner / review_dialog / rename_dialog 等

## 裝評（本地 OCR）鐵則

- **只走 ADB**（screencap/input swipe），不抓宿主視窗 → 對視窗縮放免疫
- 截圖端凍結偵測（2026-07-10 比照聯賽）：成員列表區（x 0.20–0.82 × rows y 帶）
  灰階 64×32 縮圖，diff<2 連 2 幀＝捲到底 → 提前收機（halt=list_frozen、ADB 即斷，
  歷史 18 場實測平均省 ~9 頁）；**先看過一次真捲動才武裝**——開場滑動可能被遊戲
  吃掉導致連幀相同（20260523_230338 開頭連 3 幀），未武裝的凍結幀照常入列，
  「列表完全不動」的病態情況仍由 consumer 舊 stuck/idle 邏輯兜底；
  **不可比整張畫面**：列表外有動畫，真凍結時全畫面 diff 仍卡 ~2.1–2.3 門檻邊緣
- OCR：v5-mobile 主掃＋v5-server 對低信心列 ROI 重讀（hybrid）；**cls 必關**
  （cls 會 180° 翻轉：`999999` 讀成 `666666` 且信心 1.00 的血淚教訓）
- Excel merge 不可變式：列順序不動、不刪列、`遊戲ID`(correct_nickname) 永不覆寫、
  同日取 max；fuzzy 命中一律需人工確認；未匹配紅列只在 CLI/legacy
  （append_unmatched=True）才追加，GUI 流程一律不自動寫未匹配；
  載入時跑 `duplicate_nickname_issues()`（重複遊戲ID 只有第一列配得到，GUI 警示）
- review dialog 兩區（2026-07-10 由三區合併）：❶ 沒掃到（fuzzy 提案行內
  附 OCR meta＋縮圖，checkbox 只顯示固定格式「套用: 數字」——變長文字
  會推歪欄位；六欄以 `_configure_missed_columns` minsize 對齊，行內縮圖
  _M_THUMB_W=690 不可超過六欄總寬）；❷ 未匹配可下拉指認為 ❶ 成員
  （比照聯賽人工指認：寫當日裝評＋prepend Last_OCR_ID，走
  `ReviewDecisions.assigned_unmatched` → `apply_review_decisions`，
  與勾套用同語意；預設忽略＝不寫入）。同一成員一次掃描只能有一筆：
  套用/手填/指認互斥，選擇時警告、確認時擋下
- Last_OCR_ID **多值**（2026-07-10 比照聯賽）：exact/fuzzy 確認寫入都是 prepend
  ＋正規化去重、最新在前、上限 8；空 nickname 不清既有變體；舊單值檔天然相容；
  GUI 顯示 fallback 一律取最新變體（`primary_ocr_id`），整串只進 Excel cell
- 日期欄保留上限 `MAX_DATE_COLUMNS=10`（2026-07-10）：`save()` 進場先
  `prune_capture_days()` 刪最舊日期（按日期值排序、非欄位順序），peak 先
  折入最高裝評再刪；save 整本重建所以剩餘欄自動左靠、不留空欄；備份在
  prune 前先拍所以完整歷史在 backups/；趨勢圖/上次紀錄等讀取端只看得到
  最近 10 天（週線圖約兩週）——這是保留策略的預期後果
- 裝評 dedup key = 裝評數字（實測 100% 準）；gear=0 是真值，不是錯誤

## 聯賽（Gemini）設計

- **五個畫面**：主戰場×(輸出/輔助)＋副戰場×(輸出/輔助/戰略)。四個數值欄位 x 帶
  五畫面完全相同（實測 1920×1080：~0.18/~0.234/~0.31/~0.391；名稱 0.05–0.165；
  列距 0.081h；資料區 y 0.34–0.90；參戰人數在 cx≈0.14, cy≈0.19）
- 欄位語意：輸出=擊殺/助攻/玩家傷害/建築傷害；輔助=治療/承傷/死亡/復活；
  戰略（副場限定）=小怪/修旗/王的最後一擊/王的傷害
- **拍攝/分析分離**（比照裝評）：capture 只截圖+短滑動（0.25h≈3列、750ms 慢拖，
  本人釘底列是條件性的所以必須短滑）＋左表區凍結偵測（64×32 灰階 diff<2、連凍2頁停，
  周圍雲朵會動所以不能整張比）；辨識在背景 thread、跨畫面 Semaphore(1) 序列化
- **Gemini**：structured JSON（名字+四欄整數+參戰人數）、temperature 0、
  K/M/B 換算與補 0 由模型做；prompt 要求忽略右半敵方/浮動通知/釘底本人列/邊緣裁切列、
  繁體優先但外語照實、留意 002/003 形近數字；429/500/503/504 指數退避重試 5 次
  （503=官方過載非 RPM 超量），重試訊息會回拋到 GUI 卡片；參戰人數取各頁**眾數**
  （單頁誤讀不會蓋掉正確值）
- **去重**：頁內指紋 (玩家傷害,建築傷害)/(治療,承傷)/(王傷,小怪)（兩者皆非零才用，
  否則退名字 key）→ 合併時取較長名字 → `_final_collapse`：同數據(含≥1000值)合併、
  同名且 metrics 相容(共享≥1000值或≥3欄相同)才合併——**防岡本002/003 形近名誤併**；
  下游 `_merge_views` 跨視角合併同樣防相撞：同名但共有欄位數值衝突（＝同視角雙胞胎）
  → key 消歧 name#2 保留兩列，第二列進人工指認（跨視角欄位不重疊、不受影響）
- **名冊 = data/league_scores.xlsx**（與裝評完全脫鉤；ID/遊戲ID/職業/Last_OCR_ID；
  遊戲ID 空白列＝空位自動略過）。掃後只回寫 Last_OCR_ID（**多值**：｜分隔、最新在前、
  上限 8（2026-07-09 自 3 調升：一場五畫面可能貢獻五種拼字）、只寫 exact match；
  OCR信心欄 2026-07-07 廢除）；就地改 cell 保留使用者排版、先備份。
  回寫以 ID 欄為 key → 讀取時跑 `roster_issues()` 驗證（ID 重複/成員列 ID 空白），
  GUI 與 CLI 都會提示先更新名冊
- **快照輸出**：每按一次確認寫入＝一份新檔 `league_scores_YYYYMMDD_HHMMSS.xlsx`
  （2026-07-08 檔名加秒：同一分鐘內按兩次確認曾會覆蓋前一份）
  （單一寬表「聯賽戰績」：ID/遊戲ID/職業/參與(主/副/主+副)＋主-8欄＋副-12欄；
  紅列=待人工）。疑慮淺紅標示（2026-07-10）：有參戰但該戰場全欄位 0（空值視同 0）
  ＝該戰場**有值**的 cell 標淺紅（空 cell 不標，部分掃描的未掃畫面不誤標）；
  參與 cell 標淺紅條件——只打一場看該場、兩場都打需兩場皆疑慮；有成員但參與
  空白＝參與 cell 留空但標淺紅。review 對話框按畫面分五組、不關窗可重複寫（每次 deep-copy）；
  **關窗且至少成功寫入一次＝該場消費完畢**：panel 收到 on_closed(wrote_any=True) 會
  `runner.reset()`＋重置五張卡片，防止上一場沒重拍的畫面殘留混進下一場的輸出
  （沒寫入就關窗則 scans 保留，可再按產出結果）。
  人工指認允許**多個名字指給同一成員**（不同畫面對同一人的拼字辨識常不同，各畫面
  數據落在不同欄位；2026-07-09 放寬），只擋「同畫面兩列指給同一人」（會互相覆蓋）
- 對帳：每個掃過的畫面一行（參戰人數 vs 辨識 vs 自動對應），差額/待確認紅字；
  另核對 merged（合併後列數）vs 辨識列數，不相等＝合併異常紅字（最後一道網）
- 部分掃描 OK；拍攝/分析進行中「產出結果」禁用
- 工具選單：重新分析裝評截圖（members.json / 重跑 OCR）、重新分析聯賽截圖
  （從 data/league_captures/<ts>_<bf>_<view>/pages 重跑，不需開遊戲）

## config.ini 佈局（發佈時使用者的設定入口）

`[paths]` 裝評路徑；`[ocr]` 裝評 OCR 模型（只影響裝評）；
`[league]` output_dir/roster_path；`[gemini]` model/api_key（聯賽辨識；
key 順序：config → env GEMINI_API_KEY → 開發用 .gemini_key）；
`[google_sheet]` sheet_url（機密）＋ oauth_client_id/secret（順序：config →
env GOOGLE_OAUTH_CLIENT_ID/SECRET → 開發用 .google_oauth_client.json；
build_exe.ps1 打包時會自動把該 json 注入發佈包的 config.ini）。
模型名稱一律可設定、不寫死。
**repo 在 GitHub 上是公開的**：sheet_url／client_secret 只能放 gitignored 檔或
發佈包。本機 config.ini 已設 `git update-index --skip-worktree`（2026-07-08）：
工作目錄的 config.ini 含真實 sheet_url，git 永久忽略其改動；日後要改「模板」
需先 `--no-skip-worktree`、清空機密、commit、再放回機密並重設 skip-worktree。

## 已知限制與技術債

- **裝評工作簿 save() 是整本重建**（storage/excel.py）：使用者在 guild_scores.xlsx
  自行新增的欄位／工作表／儲存格格式會在下次寫入時消失（聯賽名冊則是就地改 cell
  保留排版，兩者行為不同）。README §3.1 已加警語；長期可考慮改就地編輯
- `on_close` 會 `adb kill-server`（清掉背景 adb.exe）：屬全域動作，同機若有其他
  ADB 工具或第二份本工具正在跑會被斷線
- 戰略頁數值太小，指紋常退化成名字 key → 名字誤讀可能裂成兩列（對帳差額會抓到）
- 單一免費 Gemini key 內嵌發佈＝共用 RPM/可被抽出——發佈前要有配套
- 名冊同步的 OAuth 同意畫面若停在 GCP「測試中」狀態，refresh token **7 天過期**
  （幹部每週要重新授權）——需在 Cloud Console 把發佈狀態切成「正式版」；未驗證
  應用程式會多一個「進階→繼續」警告畫面且上限 100 使用者（公會工具夠用）
- 名冊同步的 diff 是以讀檔當下的列號寫回：diff 與套用之間若使用者手動增刪列，
  寫入位置會偏移（apply 前有比對現值防呆，但非完全防護）
- 裝評 scan_runner 以 monkeypatch 方式 hook `session._merge`（脆弱但可用）
- GUI 未防「裝評掃描與聯賽拍攝同時搶 ADB」（實務上不會同時跑）
- 掃描中使用者切走遊戲畫面無偵測（靠自律）；GUI 無「還原備份」按鈕（backups/ 有檔）
- 模擬器內 adbd 偶爾崩死（state=offline）：唯一解法是重啟該實例

## 未來可選構想（皆不在目前範圍）

Discord webhook 摘要、裝評/聯賽趨勢圖表、多公會支援、自動排程、
聯賽跨場彙整工具（快照是一場一檔，跨週分析要另做）、
missed/unmatched 的 fuzzy 推薦下拉。

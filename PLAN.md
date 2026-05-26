# RO_GearSync 計畫書

> RO 仙境傳說：世界之旅 — 公會成員裝備評分自動擷取工具

---

## 1. 專案目標

將公會幹部目前「手動翻看公會成員資訊、手動將每個人的裝備評分填入 Google 雲端 Excel」的流程自動化。最終產出一個 **單一執行檔（.exe）**，任何公會幹部只要：

1. 在自己的電腦上開啟雷電模擬器並啟動 RO 手遊。
2. 手動進入「公會 → 成員列表」畫面。
3. 雙擊 `RO_GearSync.exe`。

工具便會自動擷取所有成員的暱稱與裝備評分，將結果寫入本地 `.xlsx`（放在 Google Drive 同步資料夾即可自動上雲），並保留歷史紀錄供日後分析裝評變化。

---

## 2. 核心需求摘要

| 類別 | 需求 |
|------|------|
| 環境 | 單一 `.exe`，不需安裝 Python / 模型 / Java；目標機器只要有雷電模擬器與遊戲即可 |
| 模擬器相容 | **不受雷電視窗縮放與位置影響**（採用 ADB 與裝置直接溝通，不抓宿主螢幕） |
| 影像辨識 | 全部 **本機推論**，無雲端 API、無額度限制、無網路依賴 |
| 資料保存 | 本地 `.xlsx`；每次擷取**新增一欄日期**（寬表格） |
| 暱稱處理 | 自動偵測暱稱變更／OCR 誤差，無法自動配對時跳出視窗讓使用者手動修正 |
| 操作模式 | 半自動：使用者先進到成員列表，再啟動工具 |
| 防呆 | 模擬器未開、ADB 連不上、未進到成員列表、辨識率過低、Excel 被開啟中 … 等情境皆需有清楚錯誤訊息 |

---

## 3. 技術架構總覽

2026-05-20 起 GUI (customtkinter) 已上線作為主要入口 (`scripts/run_gui.py`)，CLI 腳本 (`scripts/probe_capture.py` + `scripts/bootstrap_excel.py`) 留下作為除錯與重跑用。下面的箱型圖反映目前已成形的層級。

```
+--------------------------------------------+
| Orchestration                              |
|   - scripts/run_gui.py        (GUI 主入口)  |
|     └── src/ro_gearsync/gui/app.py         |
|   - scripts/probe_capture.py  (CLI 擷取)    |
|   - scripts/bootstrap_excel.py (CLI 合併)   |
|   - scripts/migrate_excel_v2.py (v1→v2)     |
+--------------------------------------------+
            |
            v
+----------------------------+
| Capture Layer (ADB)        |   <-- 與雷電溝通的關鍵
|   - adb connect 127.0.0.1  |
|   - adb shell screencap    |   <-- 直接拿「裝置內」畫面
|   - adb shell input swipe  |       (跟模擬器視窗大小無關)
|   - producer/consumer 管線  |   <-- src/ro_gearsync/capture/session.py
+----------------------------+
            |
            v
+----------------------------+
| Vision Layer               |
|   - layout.py: ROI 比例     |
|   - ocr.py: RapidOCR 封裝   |
|   - parser.py: 行解析 + 重 OCR |
+----------------------------+
            |
            v
+----------------------------+
| Matching Layer             |
|   - normalize_for_match    |   NFKC + 去標點 + casefold + OpenCC t2s
|   - rapidfuzz max(ratio,…) |
|   - 四階段配對 (exact x2 → fuzzy x2 → new)|
+----------------------------+
            |
            v
+----------------------------+
| Persistence Layer          |
|   - openpyxl 寫入 .xlsx    |   寬表格，每次擷取追加 3 欄
|   - 自動時間戳備份          |   data/backups/guild_scores_YYYYMMDD_HHMMSS.xlsx
+----------------------------+
```

### 3.1 為什麼要用 ADB 而不是抓螢幕

雷電模擬器內建 ADB 服務（預設 port `5555`，多開為 `5557`、`5559` …）。透過 ADB：

- `adb shell screencap -p` 取得的是**裝置內部解析度**（雷電預設 `1280x720` 或使用者自設值），與宿主視窗顯示大小完全無關。使用者把模擬器視窗縮成郵票或全螢幕，擷圖結果一模一樣。
- `adb shell input swipe` 用裝置內座標下指令，不需要算螢幕 DPI、不需要把視窗帶到前景、不會被 Windows 滑鼠中斷。
- 不需要 `pyautogui` 那種點宿主螢幕的方式，使用者一邊跑工具一邊用電腦做別的事也不會影響。

這從根本上解決了「大家電腦環境不同、隨時可能縮放視窗」的問題。

### 3.2 主要相依套件

| 套件 | 用途 | 備註 |
|------|------|------|
| `opencv-python` | 影像處理 / ROI 裁切 / 去雜訊 | parser.py、reanalyze_session.py |
| `numpy` | 影像陣列 | OpenCV 必備 |
| `Pillow` | 影像 I/O | OCR 前處理 |
| `rapidocr` 3.x (ONNX Runtime 後端) | OCR（見 §6） | 完全本機推論；取代舊版 `rapidocr-onnxruntime` |
| `onnxruntime` | OCR 推論引擎 | RapidOCR 的執行底層 |
| `rapidfuzz` | 暱稱模糊比對 | 比 fuzzywuzzy 快很多 |
| `opencc-python-reimplemented` | 繁簡轉換 | 比對前統一成簡體，吸收 OCR 繁/簡誤判 |
| `openpyxl` | Excel 讀寫 | 保留欄位格式、染色 |
| `customtkinter` | GUI（M6 才會用） | tkinter 內建，customtkinter 較美觀 |
| `pyinstaller` | 打包 | onefile 模式 |
| `loguru` | 結構化 log | 方便支援使用者除錯 |

### 3.3 與雷電模擬器的連線策略

1. **ADB binary**：直接打包 `adb.exe`（約 5MB），不依賴雷電自己的 `adb.exe`，避免使用者雷電版本差異。
2. **Port 探測**：依序 try `5555 / 5557 / 5559 / … / 5585`，第一個成功 `adb connect` 並回應 `getprop` 的就採用。
3. **手動覆寫**：GUI 提供「手動指定 ADB port」欄位，給特殊環境的使用者。
4. **連線失敗回饋**：詳細的步驟引導圖（雷電 → 設定 → 其他設定 → 開啟 ADB 偵錯）。

---

## 4. 資料結構設計

### 4.1 主 Excel：`guild_scores.xlsx`（v2 schema，2026-05-20 之後）

**寬表格格式**：左半部 6 個 meta 欄固定，右半部每天一個日期欄（只記裝評）：

| ID | correct_nickname | latest_ocr_nickname | confidence | review_reason | 最高裝評 | 2026-05-18 | 2026-05-19 | 2026-05-20 |
|---|---|---|---|---|---|---|---|---|
| 1 | 杰尼衰 | 杰尼衰 | 0.96 |  | 48,563 | 47,107 | 48,563 | 48,800 |
| 2 | p | p | 0.50 | new_member | 50,721 |  | 50,721 |  |

設計重點：

- `ID` 是**使用者手動維護的成員識別碼**（column A，2026-05-22 起）：
  - **完全唯讀 / 唯寫回** — 程式只讀 column A、save 時把使用者填的值原封寫回。**從不自動編號、從不自動補空白、從不 compact**
  - **新成員 (phase 5 append) column A 留空白** — 使用者打開 Excel 手填，通常是把退會者那一格的 ID 數字搬到新成員列，這樣編號維持公會編制不變
  - **既有列順序固定**：load 時略過完全空白的列，但保留有任何資料（含只有 ID）的列；save 寫回時不重排
  - **改名 / merge / fuzzy 都不影響 ID** — `correct_nickname` 改，`player_id` 不動
  - **GUI 即時掃描表格的 `#` 欄顯示這個 ID** — 配對成功的列 `#1, #42, #148`，pending/未配對的列顯示 `—`
- `correct_nickname` 是**使用者親手填的真值**，匹配優先採信此欄；任何流程都不得寫入。GUI 的「改名」對話框是唯一合法寫入這欄的入口。
- `latest_ocr_nickname`：最近一次 OCR 認到的字串；用於下一次配對。改名動作會把此欄清空。
- `confidence`：對應 `latest_ocr_nickname` 的 OCR 信心 (0–1)。改名動作會清空。
- `review_reason`：非空字串就要使用者人工複核
  （`low_ocr_confidence` / `missing_nickname` / `new_member` / `unmatched_after_capture` / `missed_this_capture`）。
- `最高裝評`：**使用者可手動覆寫的高水位線**（2026-05-22 起改成儲存欄位而非計算屬性）：
  - 設計理由：使用者常把比可見日期欄更早的歷史峰值手動填進這格（公會幾個月前的紀錄不會在 schema 裡）
  - **新捕捉只往上突破才更新** — `record_gear_for_day` 內 `if new > peak: peak = new`，較低的捕捉值絕不會蓋掉手填峰值
  - **載入時自我修正** — 若該玩家某日期欄已經有高於 peak 欄的值（資料不一致），peak 自動上修為日期欄最大值
  - 改名 / merge 流程都不會清掉這個欄位
- **日期欄一天只有一個**：欄頭就是 `YYYY-MM-DD`，格子內容是該天看到的「最高」裝評。同一天再次掃描只會在比目前值高的時候覆蓋；低於現值則維持不動。
- `new_member` 與 `missed_this_capture` 兩種列會被整列染紅，使用者打開檔案一眼就看到要處理。

#### v1 → v2 遷移

舊版 schema 用「裝評 / 貢獻 / 活躍 YYYY-MM-DD HH:MM」三欄一組、含 `known_aliases`，並把 `peak_gear_score` 英文做欄名。`scripts/migrate_excel_v2.py` 把這些攤平：

* 把每天多次的「裝評 ... HH:MM」欄取 max 收成一欄 `YYYY-MM-DD`
* 移除 `known_aliases` / `貢獻` / `活躍` 欄
* 保留 `correct_nickname` 真值不動
* 動手前自動備份原檔到 `data/backups/`

### 4.2 別名字典（已從 schema 移除）

舊版 `known_aliases` 欄與配套的 `aliases.json` 計畫一起作廢。實測 150 人公會
跑了多天，OCR 對同一玩家通常吐相同字串，`latest_ocr_nickname` + `correct_nickname` 雙欄配上 fuzzy 比對已足夠，多維護一份別名清單沒帶來新的配對率。

### 4.3 設定檔：`config.ini`（已實作，2026-05-21）

放在 .exe / 專案根目錄旁邊，文字編輯即可修改。GUI 啟動時讀一次、按「重新偵測」會再讀一次（不需重啟）。第一次跑時自動產生帶註解的範本，所有 key 留空 = 用內建預設。

```ini
[paths]
ldplayer_dir   = <空 = 自動搜尋 C:\LDPlayer\... / C:\Software\LDPlayer\... 等>
workbook_path  = <空 = <app>/data/guild_scores.xlsx>
data_dir       = <空 = <app>/data>
logs_dir       = <空 = <app>/logs>
```

實作位於 [`src/ro_gearsync/utils/config.py`](src/ro_gearsync/utils/config.py)。`paths.py`
的 `adb_binary()` / `ldconsole_binary()` / `user_data_dir()` / `logs_dir()` /
`default_workbook_path()` 全部會先看 config，再走 auto-detect / 預設值。

CLI 腳本仍維持 `argparse`（一次性參數），不會強制吃 config.ini —
適用情境不同。

### 4.4 自動備份（已實作）

`GuildScoresWorkbook.save(backup=True)` 在寫入前自動複製成
`data/backups/guild_scores_YYYYMMDD_HHMMSS.xlsx`。
**目前沒做輪替/上限**，所有備份都會留下；用戶自行清理。將來若磁碟壓力出現再加保留筆數。

---

## 5. 工作流程（半自動）

詳細的 UI 狀態機與每個對話框長相見 §8。這裡只列骨幹順序。

```
[使用者]                          [工具]
   |                                 |
   | 1. 開啟雷電，啟動 RO            |
   | 2. 雙擊 RO_GearSync.exe         |
   |-------------------------------->|
   |                                 | 3. 環境自動檢查
   |                                 |    a. 偵測雷電是否啟動
   |                                 |    b. 偵測 ADB 偵錯是否打開
   |                                 |    c. 偵測有幾個 LDPlayer 實例
   |                                 |       (多實例 → 下拉選；單實例自動選)
   |    若有任何缺失：                |
   |<--------------------------------|  逐項提示 + 教學圖文 + 重新偵測
   |                                 |
   | 4. 選擇 Excel 檔                 |
   |    (新建 / 已存在)               |
   |-------------------------------->|
   |                                 | 5. 讀 Excel 取得既有成員清單
   |                                 |
   |    詢問：「上次擷取後有人改名嗎？」|
   |<--------------------------------|
   | 6a. Yes → 開啟改名登記對話框 →   |
   |    填好「Excel 舊名 → 新名」      |
   |-------------------------------->|  立即寫回 Excel（更新 correct_nickname
   |                                 |  與 known_aliases），自動備份原檔
   |                                 |
   | 6b. No  → 跳過                  |
   |                                 |
   | 7. 提示：「請在雷電中切到公會     |
   |    成員頁、滑到最上方，完成後按   |
   |    開始掃描」                     |
   |-------------------------------->|
   |                                 | 8. 再次截圖驗證是否在成員列表
   |                                 |
   |                                 | 9. 啟動雙 thread 管線（§8.6）：
   |                                 |    Producer: ADB 截圖 + 滑動
   |                                 |    Consumer: OCR + 比對 Excel
   |                                 |    UI thread: 即時顯示
   |                                 |
   |    UI 即時顯示：                 |
   |<--------------------------------|  - 進度條 / 預估剩餘時間
   |                                 |  - 每偵測到一人就跳一行：
   |                                 |    「✅ 杰尼衰 47107」對到 / 紅底「🟥」未對到
   |                                 |  - 「掃描中請勿操作雷電」紅字
   |                                 |
   |                                 | 10. 滑動到底（連 2 頁無新成員）→ 結束
   |                                 |
   |    顯示「兩份未處理清單」對話框：  |
   |<--------------------------------|  A. Excel 有但這次沒抓到的人
   |                                 |  B. OCR 抓到但 Excel 對不上的人
   |    使用者逐筆處理（合併、新成員、 |     (含 rapidfuzz 自動推薦改名候選)
   |    退會、略過）                  |
   |-------------------------------->|
   |                                 | 11. 寫入 Excel 新增日期欄
   |                                 |     + 更新別名 + 更新 peak_gear_score
   |                                 |     + 自動備份
   |                                 | 12. 顯示摘要 + 「開啟 Excel」按鈕
   |<--------------------------------|
   | 13. 確認 / 結束                  |
```

### 關鍵子流程：滑動偵測「到底」

- 每次滑動後截圖，比對前後兩張畫面的「最末三列」是否完全重疊（hash 或 SSIM）。
- 連續兩次「滑動後無新內容」即判定到底。
- 安全上限：最多滑動 100 次，避免無窮迴圈。

### 關鍵子流程：滑動量

- 不是滑「一個視窗高度」，而是滑「視窗高度 × 0.6」，確保前後兩張有 40% 重疊區，方便去重與斷點偵測。
- 滑動速度刻意放慢（duration ≥ 600ms），避免遊戲認定為「快速滑動」觸發慣性，導致跳過列。

---

## 6. OCR 引擎（已驗證選型）

採用 `rapidocr` 3.x（ONNX Runtime 後端的 PaddleOCR 模型）。**已用 150 人完整公會做過三方對照**：

### 6.1 三方對照結果（同一組 37 張截圖）

| 變體 | 模型大小 | 每頁速度 | 缺暱稱 | 低信心 | 平均信心 |
|------|---------|---------|--------|--------|---------|
| PP-OCRv4 mobile（舊基準）| 15 MB | 5.0s | 3 | 19 | 0.887 |
| **PP-OCRv5 mobile（主用）** | **20 MB** | **2.6s** | 6 | 18 | 0.879 |
| **PP-OCRv5 server（fallback）** | **200 MB** | 57s | 2 | 8 | 0.922 |

**關鍵發現**：PP-OCRv5 不論 mobile/server 都**完全解決繁簡混淆**（来→來、当→當、烟→煙、网→岡、绒→絨 等 25+ 例修正）。對玩繁體中文版 RO 的這個公會差距顯著。

### 6.2 採用的雙層 OCR 策略

```
每張截圖：
  ├─ Pass 1: v5-mobile 全頁 OCR (2.6s)
  │   ├─ 大部分行 → 信心 ≥ 0.80 → 直接採用
  │   └─ 信心 < 0.80 或完全沒抓到 → 標記 fallback
  │
  └─ Pass 2 (僅針對標記行): v5-server 對該行 ROI 裁切後 re-OCR (~1s/行)
      └─ 採用較高信心或較長有效字串的結果
```

37 頁實測：~10 筆需要 fallback × 1s = 額外 10s → 整體仍在 110s 內，遠快於純 server 的 35 分鐘。

### 6.3 為什麼不選別的引擎

| 候選 | 否決理由 |
|------|---------|
| **PaddleOCR 完整版** | 連帶 PaddlePaddle 框架 200MB+，打包後 .exe >500MB |
| **Tesseract** | 對遊戲字型（描邊、彩色背景）辨識率遠遜於 PaddleOCR 系列 |
| **EasyOCR** | 中文準確度不如 PP-OCRv5，推論慢 |
| **TrOCR (Microsoft)** | 模型 350MB+，推論極慢，CJK 不一定贏 v5 |
| **chinese_cht_PP-OCRv3** | 純繁體模型，但停在 v3 架構，被 v5 全面取代 |
| **雲端 OCR (Google/Azure)** | 違反「免費 + 本機 + 不限額度」需求 |

### 6.4 影像前處理（每張截圖都跑）

| 處理 | 目的 |
|------|------|
| 裁切 ROI（暱稱欄、裝評欄分開）| 減少 OCR 受 UI 干擾 |
| 2~3x 上採樣（fallback 時）| 提升小字辨識 |
| 數字欄正規化 `re.sub(r'[^\d]', '', text)` | 裝評欄純數字保險 |
| 字元黑名單（`@?©®O` 懸空字尾）| 清掉 v5 偶爾的尾部雜訊 |

### 6.5 永遠關閉 cls — 學到的代價昂貴的一課 (since 2026-05-19)

RapidOCR 預設會跑 **cls（orientation classifier）**，這個步驟判斷文字方向並在需要時把整列翻 180°。對遊戲 UI 文字而言，這完全沒必要 — UI 從不會倒過來印字。但 cls **誤判**這件事本身會釀成嚴重後果：

**實例 1（數字字型）**：玩家暱稱 `__999999`，OCR 連續多版本變體都讀成 `666666` 信心 1.00。診斷後發現 cls 認為這列倒過來了，自動翻 180° 後送進 rec — 而**這個遊戲花俏的數字字型，「9」翻轉 180° 後與「6」像素層級完全一致**。所以 rec 完美地讀出「6」，並合理地給 1.00 信心。

**實例 2（中文字）**：玩家暱稱 `瘋狂暗魔陰帝`，OCR 給 `狂暗凰陰帝` 信心 0.62。同樣是 cls 翻轉 + rec 重讀的副作用 — 翻轉後的「魔」像素與「凰」相近、「瘋」翻轉後不像任何標準字所以被丟。

**結論**：`OcrEngine` 預設 `use_cls=False`。`Global.use_cls: false` 寫進 params 強制覆寫 RapidOCR 預設值。從沒看過遊戲畫面有任何倒置文字；cls 對我們純粹只有副作用沒有好處。

**意外的好處**：跳過 cls 階段，每頁 OCR 時間從 16.9s → 12.5s（v5-mobile + v5-server fallback hybrid，37 頁總時長從 626s → 460s，**減少 27%**）。

---

## 7. 暱稱比對與容錯（這是工具的核心難題）

### 7.0 鐵則 — Excel 列管理 (updated 2026-05-19)

使用者已經手動確認過大量 `correct_nickname` 的真值。為了不破壞這份工作：

1. **既有列順序永遠不動** — 不 sort、不重排
2. **既有列永遠不刪除** — 即使「公會上有但這次沒抓到」也只放進 `[C] MISSED` 報告
3. **`correct_nickname` 欄永遠唯讀** — 任何流程都不得寫入此欄
4. **完全配對不到的 OCR 結果會「追加」到 Excel 最下方，整列染紅** — 使用者打開 Excel 直接看到紅色列 = 需要您填 `correct_nickname`（可能是新成員，或是 OCR 太離譜得另外用 alias 處理）

新成員不再需要使用者手動先補列再掃 — 系統會自動追加並染紅，使用者只需填入正確暱稱。

### 7.1 四階段配對（updated 2026-05-19）

配對採嚴格的四階段流程，每階段都以「上一階段沒被認領」的列為候選集，避免低信心配對提早搶位：

```
Phase 1 — exact correct_nickname
  使用者填的真值完全符合（normalize 過後）→ 鎖定
  normalize：NFKC + 去裝飾標點 + casefold + OpenCC 繁→簡

Phase 2 — exact latest_ocr_nickname
  上次擷取的 OCR 結果完全符合 → 鎖定
  （OCR 對同一玩家通常吐相同的「錯誤」字串，這就是天然 alias）

Phase 3 — fuzzy correct_nickname (≥ 65)
  跟還沒被認領的列的 correct_nickname 做 rapidfuzz max(ratio,
  partial_ratio, token_set_ratio) 比較

Phase 4 — fuzzy latest_ocr_nickname (≥ 65)
  跟還沒被認領的列的 latest_ocr_nickname 做同上 fuzzy

Phase 5 — append (這次配不到任何人)
  追加新列，review_reason=new_member，背景染紅
  使用者打開 Excel 直接看到 → 填入正確暱稱
```

Phase 3/4 中分數 65~79 的會被自動配對但同時標 `review_reason=unmatched_after_capture` 提醒使用者複核；80+ 視為穩配對。

### 7.2 三份報告（merge 完成必輸出）

每次 merge 結束強制輸出三個區塊讓使用者複核：

* **[A] REVIEW** — phase 2 落在 65~79 自動配對的，標出 OCR 結果 / 配到誰 / 信心 / 前 3 名替代。`review_reason` 欄被填上提示文字
* **[B] UNMATCHED captures** — OCR 看到但沒有 Excel 列超過 65 分。**不寫入工作表**。可能是新成員、改名後找不到對應、或 OCR 雜訊
* **[C] MISSED in capture** — Excel 上有但本次沒被認領的列。**不修改該列**。可能是退會、OCR 漏看、或滑動沒到底

使用者拿這三份手動處理：B 通常是 manually 在 Excel 加列、補 `correct_nickname`；C 通常是把對應列的 `known_aliases` 多加一筆別名。

### 7.3 為什麼需要模糊比對（保留歷史脈絡）

OCR 在中英日混合 + 表情符號 + 全形/半形標點的暱稱上，常見錯誤：

| OCR 結果 | 真實暱稱 | 錯誤類型 |
|----------|----------|----------|
| `阿明` | `阿明` | （無誤） |
| `阿朋` | `阿明` | 形近字 |
| `Niсkname` | `Nickname` | 西里爾字母 с / 拉丁 c 混淆 |
| `阿明 ` | `阿明` | 尾隨空白 |
| `阿明★` | `阿明☆` | 全形符號變體 |
| `ABC123` | `ABC 1 2 3` | 空白判斷錯誤 |

### 7.2 多階段比對策略（舊版草稿 — 已由 §7.1 取代）

> 下表為 v0 草稿，門檻 85 / 70 / <70 與 stage 排序與目前 `matcher.py`
> 採用的 65 / 80 / 95 四階段流程不一致。保留作為設計演進的脈絡。
> **以 §7.1 為準**。

```
OCR 結果 "阿朋"
   ↓
[Stage 1] 正規化：strip、全形→半形、移除標點/表情、Unicode NFKC
   ↓
   標準化後 "阿朋"
   ↓
[Stage 2] 精確比對：在 current_nickname 或 aliases 找完全相符
   ↓ 沒命中
[Stage 3] 模糊比對（rapidfuzz）：
   候選池 = 所有 current_nickname + 所有 aliases
   分數 = max(token_set_ratio, partial_ratio, 編輯距離 score)
   閾值 ≥ 85 → 自動配對
   閾值 70~84 → 加入「待確認」清單（半自動，預設選最高分但讓使用者複核）
   閾值 < 70 → 視為新暱稱，加入「未知」清單
   ↓
[Stage 4] 衝突解決對話框（GUI）：
   逐筆顯示「OCR 結果 + 截圖該列縮圖 + 最有可能的 3 個候選 + 新成員選項」
```

### 7.3 正規化規則細節

```python
def normalize_nickname(text: str) -> str:
    # 1. Unicode 正規化（全形 → 半形）
    text = unicodedata.normalize("NFKC", text)
    # 2. 去掉空白與控制字元
    text = re.sub(r"\s+", "", text)
    # 3. 去掉常見裝飾符號
    text = re.sub(r"[★☆♥♡♪♫※●○◆◇■□▲△▼▽]", "", text)
    # 4. 小寫化（針對英文部分）
    text = text.lower()
    return text
```

> 注意：正規化只用於「比對」，**儲存時保留原始 OCR 結果**，避免遺失資訊。

### 7.4 衝突解決 UI 設計

當有未知或低信心的暱稱時，彈出單一視窗逐筆處理：

```
┌─────────────────────────────────────────────────────────┐
│  發現未登錄的暱稱  (3/5)                                 │
├─────────────────────────────────────────────────────────┤
│  OCR 結果：    阿朋                                       │
│  該列截圖：    [小圖縮圖：阿朋    13,500]                  │
│                                                          │
│  最相似的現有玩家：                                       │
│    ○ P0007  阿明     (相似度 88%, 上次裝評 13,000)       │
│    ○ P0023  阿朋朋   (相似度 75%, 上次裝評 11,200)       │
│    ○ P0045  阿月     (相似度 62%, 上次裝評 8,900)        │
│                                                          │
│    ○ 這是新加入的成員                                     │
│    ○ 略過（OCR 誤判，不寫入這筆）                         │
│                                                          │
│  [上一筆]  [跳過全部]  [下一筆 →]                         │
└─────────────────────────────────────────────────────────┘
```

使用者選擇後：
- 若是現有玩家 → 將 OCR 結果加入該玩家的 `aliases`，更新 `current_nickname`。
- 若是新成員 → 配發新 `player_id`。
- 若是誤判 → 記到 `logs/ocr_misses.txt`，不寫入 Excel。

---

## 8. GUI 設計與使用流程

採用 `customtkinter`（純 Python、外觀現代）。整個 GUI 圍繞一條**狀態機**走，每個狀態 UI 都會清楚反映目前處在哪一步、下一步該做什麼。

### 8.0 v1 GUI 已實作範圍（2026-05-21 起，2026-05-20 首版）

入口：`scripts/run_gui.py`。實作位於 `src/ro_gearsync/gui/`：

| 檔案 | 角色 |
|---|---|
| `gui/app.py` | 主視窗 `RoGearSyncApp`：環境檢查 / Excel 載入 / 工具列（改名 → 開始 → 中止）/ 即時表格 / 統整對話框 / 重新掃描 menu |
| `gui/scan_runner.py` | `ScanRunner` + `resolve_captures()`：worker thread 包 `CaptureSession`、phase-A exact + phase-B fuzzy、事件透過 `queue.Queue` 回主 thread |
| `gui/rename_dialog.py` | 改名 modal：可搜尋成員（只比對 correct_nickname）→ 表格化清單（含近 3 天裝評框線）→ 輸入新名 → 二次確認 → 寫回 workbook |
| `gui/review_dialog.py` | 結束統整 modal：A 區「Excel 有但本次沒掃到」可手填裝評；B 區「辨識到但無 Excel 對應」附 `page_NNN.png` 參考；取消/✕ 有 guard 防誤關 |

**核心掃描流程**（環境檢查 → Excel 載入 → 開始掃描 → 即時顯示 → 統整視窗 → 寫入）已可端到端跑完。

**即時掃描表格**（sticky header，column titles 不隨滾動）：

```
#    成員                                     裝評          變化
─────────────────────────────────────────────────────────────────
#08  杰尼衰                                   48,800     (+1,500)  ← 綠
#09  衰星高照                                 71,702       (+500)  ← 綠
#10  (辨識中…)                                85,493              ← 灰底，等 phase B
#11  押比鴨鴨                                 73,268       (-300)  ← 紅
#12  阿明                                     50,000        (±0)
#13  未配對 ｜ OCR: p                          50,721              ← 紅底
```

規則：

* **兩階段配對**：phase A（掃描中）只 exact match `correct_nickname` / `latest_ocr_nickname`；沒中的進 pending pool 顯示「(辨識中…)」。phase B（掃描完）對 pending 跑 fuzzy → 紅色 `fuzzy_review` 或 `unmatched`。
* 列以 `dedup_key` 為鍵，phase B 解析時就地 repaint（不會出現重複列）。
* `delta = 當前裝評 - 最近一個比今天早的日期欄裝評`（用 `PlayerRecord.previous_gear_before(today)`）。沒前一筆 → 變化欄留空，不會把裝評塞進變化欄。
* `delta > 0` 顯示綠色、`delta < 0` 顯示紅色、`delta == 0` 顯示灰色 `(±0)`。
* 視窗加寬時 `成員` 欄吸收所有額外寬度、`裝評`/`變化` 兩欄貼右邊。
* **不顯示 OCR 名字**（只在 unmatched 列以 `OCR: xxx` 副註出現）。

**結束統整對話框** (`ReviewDialog`)：

* ❶ Excel 有但本次沒掃到 — 每列顯示「上次 M/D = xx,xxx」+ 可空白的「本次裝評」輸入。確認後自動 `apply_manual_missed_entries()` 寫入。
* ❷ 辨識到但無 Excel 對應 — 顯示 OCR 名 / 裝評 / 信心 / `page_NNN.png` + **原始截圖縮圖**（從 page PNG 裁出該列水平條帶顯示為 320×34 縮圖），只供參考、**不寫入** workbook。
  - 縮圖涵蓋兩種情況：對不到任何既有玩家 / OCR 看到裝評卻判定名字為空（這類列以前會被 filter 丟掉，現在保留下來給使用者眼睛確認）
* 「取消」或 ✕：若 ❶ 區有人填了字 → 跳「確定不填就關閉？」二次確認；確認後仍會把 phase A/B 已配對成功的部分存檔，只是丟掉手填值。

**改名 / 管理成員對話框** (`RenameDialog`)：

* 工具列「改名 / 管理成員」按鈕觸發。
* 搜尋只比對 `correct_nickname`，輸入時 scrollbar 自動回到頂。
* 列表是真正的表格：`名字 (200px) | 最高評分 (100px 右對齊) | 近 3 天裝評 (每天有獨立框線格)`。
* 點「確認改名」會跳二次確認 dialog 列出「原名 → 新名」對照。
* 寫回 workbook：`correct_nickname ← 新名`、`latest_ocr_nickname` 與 `confidence` 清空、裝評歷史保留、即時 `save(backup=True)`。

**重新掃描**（menu bar）兩種模式：

| 選單項 | 用途 | 速度 |
|---|---|---|
| 重新讀取 members.json… | 載入既有 session 的 `members.json` 直接走 matching + review 流程，不跑 OCR | 秒級 |
| 重新掃描所有圖片… | 對 session 的 `pages/page_*.png` 重跑一次完整 OCR 流程（v5-mobile + v5-server fallback），再走 matching | 8–10 分鐘 / 40 頁 |

兩者共用 review 對話框；後者會把新 OCR 結果額外存成 `members_reocr.json` 不覆蓋原檔。

**中止掃描**：按下「中止」會彈 confirm — 確認後 set `_cancelled`、`CaptureSession` 退出、phase B 跳過、`_handle_summary` 識別 `was_cancelled` 後跳過 review dialog 並 `shutil.rmtree(session_dir)` 把本次暫存資料夾整個刪掉。

未實作（暫不排）：環境檢查的「等待 ADB 打開」自動重試、改名登記預檢對話框（§8.3 廢棄 — 改名隨時可叫）、雷電輸入鎖定（目前只靠提示文字）。

### 8.1 狀態機總覽

```
[1] 啟動 + 環境自動檢查
        │
        ├─ 雷電未開            → 提示 + 教學圖 + 重新偵測
        ├─ ADB 偵錯未開        → 提示 + 圖文教學 + 重新偵測
        ├─ 偵測到多個實例       → 下拉選單讓使用者選（顯示雷電多開的「視窗名稱」）
        └─ 全部 OK             ↓
[2] Excel 檔案選擇
        │
        ├─ 開啟既有 .xlsx       → 讀取，記憶上次路徑
        └─ 新建 .xlsx           → 選擇儲存資料夾與檔名
        ↓
[3] 暱稱變更預檢
        │
        ├─ 「上次擷取後有人改名嗎？」
        ├─ Yes → 開啟「改名登記」對話框 → 寫回 Excel
        └─ No  → 略過
        ↓
[4] 操作引導
        │
        └─ 提示「請在雷電中開啟公會成員頁面，並滑到最上方」
           + 確認按鈕（按下後再次驗證畫面為成員列表）
        ↓
[5] 掃描中（背景 thread）
        │
        ├─ Producer thread: ADB 截圖 + 滑動
        ├─ Consumer thread: OCR + 解析 + 即時比對
        ├─ UI thread     : 進度條 + 即時偵測列表 + 阻擋遊戲操作
        ↓
[6] 結果回顧
        │
        ├─ 公會有但本次沒抓到的人（未更新清單）
        ├─ OCR 抓到但 Excel 對不上的人（未對應清單）
        ├─ 兩份清單都允許使用者操作（手填、合併、標記退會、改名歸併）
        └─ 確認後寫入 Excel + 顯示摘要
        ↓
[7] 完成（顯示「開啟 Excel」「再次掃描」「結束」三鍵）
```

### 8.2 主視窗版面（草稿）

```
┌─────────────────────────────────────────────────────────────────────┐
│ RO_GearSync                                              [⚙ 設定]    │
├─────────────────────────────────────────────────────────────────────┤
│ 環境檢查                                                              │
│   雷電模擬器：✅ 已啟動（1 個實例）                                    │
│   ADB 偵錯  ：✅ 連線正常（127.0.0.1:5555）                          │
│   實例選擇  ：[ RO-杰尼龜速/熱江 ▼ ]   （若多開才出現）              │
│   Excel 檔  ：D:\Drive\RO\guild_scores.xlsx  [📁 變更]              │
│                                                                     │
│ ─────────────────────────────────────────────────────────────────── │
│ 開始前確認                                                            │
│   ☐ 我已經確認上次擷取後沒人改名         [✏ 改名登記...]            │
│   ☐ 我已經在雷電中開到「公會 → 公會成員」並滑到最上方                │
│                                                                     │
│                  [  🚀 開始掃描  ]                                   │
├─────────────────────────────────────────────────────────────────────┤
│ 即時掃描結果                                          進度 23/37    │
│ [█████████████████░░░░░░░░░░░░] 62%   估計剩餘 1m 14s              │
│                                                                     │
│   #08  ✅ 杰尼衰         裝評 47,107  (↑ 比上次 +1,200)             │
│   #09  ✅ 衰星高照       裝評 71,702  (↑ +500)                      │
│   #10  🟥 緊張爺爺       裝評 85,493  ← 對不到既有玩家               │
│   #11  ✅ 押比鴨鴨…      裝評 73,268  (↓ -300)                      │
│   ...                                                               │
│                                                                     │
│  ⚠ 掃描中，請勿操作雷電視窗 (已在 ADB 層阻擋滑鼠/觸控)               │
└─────────────────────────────────────────────────────────────────────┘
```

紅底列 = OCR 偵測到、但對應不到 Excel 任何 `correct_nickname` / `known_aliases`。使用者**現場就能看出有問題**，不必等掃描跑完。

### 8.3 改名登記對話框

```
┌─────────────────────────────────────────────────────────────┐
│ 暱稱變更登記                                                  │
├─────────────────────────────────────────────────────────────┤
│ 上次擷取後有誰改名？把「Excel 上的舊名」對「遊戲內現在的新名」    │
│ 填好，按 + 加一行，全部填完按「儲存並開始掃描」。                 │
│                                                              │
│   Excel 舊名（從下拉選）          新名（自由輸入）              │
│   [ 阿明        ▼ ]    →   [ 大明朝               ]   [✕]   │
│   [ 衰星高照     ▼ ]    →   [ ★衰星★              ]   [✕]   │
│   [+ 加一筆改名 ]                                            │
│                                                              │
│  影響說明：儲存後會：                                         │
│   1) 把「阿明」加進「大明朝」那一列的 known_aliases             │
│   2) 把該列的 correct_nickname 更新為「大明朝」                │
│   3) 自動備份原 Excel 到 data/backups/                       │
│                                                              │
│              [取消]   [儲存並開始掃描]                        │
└─────────────────────────────────────────────────────────────┘
```

### 8.4 結束後的「未對應清單」對話框（M5 衝突解決升級版）

掃描結束後一次性顯示兩種狀況：

```
┌──────────────────────────────────────────────────────────────┐
│ 掃描完成 — 還有 5 筆需要您處理                                  │
├──────────────────────────────────────────────────────────────┤
│ A. Excel 上有但本次沒抓到的人 (3 位)                            │
│   - 阿月       上次 2026-05-11 裝評 65,800                     │
│   - 紅人      上次 2026-04-30 裝評 102,000                     │
│   - 小狗      上次 2026-05-04 裝評 88,500                      │
│   原因可能：退會 / OCR 漏看 / 沒滑到底                          │
│   [全部標為「本週缺席」]   [重新掃描補抓]                       │
│                                                              │
│ B. OCR 抓到但 Excel 對不上的人 (2 位)                          │
│   - 緊張爺爺   裝評 85,493                                     │
│     → ○ 新成員                                                │
│     → ○ 是 [ 王老五 ▼ ] 改名後的暱稱                          │
│     → ○ OCR 誤判，略過                                        │
│   - 蚩嵐佐    裝評 49,941                                     │
│     → ○ 新成員                                                │
│     → ○ 是 [ 浩蛋佐 ▼ ] 改名後的暱稱（相似度 65%）             │
│     → ○ OCR 誤判，略過                                        │
│                                                              │
│         [取消（不寫入）]    [全部處理完，寫入 Excel]            │
└──────────────────────────────────────────────────────────────┘
```

### 8.5 阻擋遊戲操作的策略

需求是「掃描期間阻止使用者操作雷電」。實作分兩層：

1. **訊息層（一定有）**：UI 強制提示「掃描中，請勿操作雷電」+ 紅底動畫
2. **技術層（盡量做到）**：
   - 雷電有 `ldconsole.exe rock --index N` 與 `lock` 系列子指令可鎖定鍵盤滑鼠輸入
   - 也可以用 `ldconsole.exe action --index N --key "Window.Bring2Top"` 把焦點搶回掃描程式
   - 若皆不可行 → fallback 為「Windows API 限制最上層視窗 + 提示文字」

掃描結束自動解鎖，並彈出確認框 OK 才解除提示。

### 8.6 即時管線並行（不可缺）

掃描過程是 **producer / consumer 雙 thread + UI thread** 的三層：

| Thread | 任務 | 阻塞點 |
|--------|------|--------|
| **Producer** | `adb screencap` → 推到 queue → `adb swipe` → 等動畫 | I/O bound（adb 通訊）|
| **Consumer** | 從 queue 取 PNG → OCR → 解析 → 比對 Excel → 推到 UI queue | CPU bound（ONNX 推論）|
| **UI**       | 從 UI queue 取結果更新進度條/列表 | 不可阻塞 |

效果（37 頁 / v5-mobile）：
- 同步版：截圖 0.8s + 滑動 1.5s + OCR 2.6s = 4.9s × 37 = 181s
- 管線版：max(2.3s, 2.6s) ≈ 2.6s × 37 = ~96s（**省 ~47%**）

實作要點：
- 用 `queue.Queue(maxsize=3)` 防止背壓
- Producer 結束時送一個 sentinel `None` 通知 Consumer 該停了
- 任何 thread crash 都要送 `Exception` 物件回 UI queue 顯示給使用者
- `customtkinter` 必須用 `.after()` 把更新排程回主 thread，不可從 worker thread 直接改 widget

### 8.7 「找不到對應的人」與「改名」的雙重防護

這是核心難題。為了「最好不要有人沒處理到」：

1. **預檢**（8.3）：使用者**主動**登記知道的改名 → 立即更新 Excel 別名
2. **掃描中即時**：紅底標出對不到的暱稱 → 使用者邊看邊知道哪些需要關注
3. **掃描後**（8.4）：兩份清單（Excel-中-沒被更新 + OCR-沒-對到-Excel）強制使用者逐一處理才能寫入
4. **rapidfuzz 提示**：對應不到的 OCR 結果與 Excel 既有清單做模糊比對，自動推薦「最可能是改名為 X」（信心 ≥ 70%）讓使用者一鍵採用

**改名工作流摘要**：
- **使用者已知改名** → 預檢登記（最乾淨）
- **使用者忘記登記** → 掃描中紅底 + 掃描後對話框會自動把疑似改名推薦給他
- **改名 + OCR 同時出錯** → fuzzy 信心 < 70% → 進「新成員 / 略過」二選一

---

## 9. 防呆與錯誤處理一覽

| 情境 | 處理方式 | 狀態 |
|------|---------|---|
| 雷電未開啟 | ldconsole 列舉多開器；每個 instance 標清楚「未啟動」 / 「ADB 尚未啟用」 / 「已連線」 | ✅ |
| ADB 偵錯未開啟 | 文字提示「請至其他設定 → ADB 偵錯 → 選擇開啟本地連接」 | ✅ |
| 多個 LDPlayer 實例 | 下拉選單顯示「[idx] 名稱  (狀態)」、可自由切換 | ✅ |
| ADB 連線成功但畫面不是成員列表 | 第一頁 sanity check：若 `parse_member_page()` < 3 列、或無一列有合理裝評（1–7 位數）→ 跳 `messagebox.showwarning` 帶引導文字並中止（自動刪 session 暫存）| ✅ |
| OCR 一列都抓不到 | 顯示「辨識失敗，可能字型過小或解析度過低」+ 建議調整模擬器解析度為 1280×720 | ⏳ |
| Excel 檔案被開啟而鎖定 | 觸發 PermissionError → messagebox 提示「請關閉 Excel 後再試」 | ✅ |
| 同一天執行兩次 | 同日欄位自動取 max；不彈窗（schema 設計使然）| ✅ |
| 擷取中途使用者切換到別的遊戲畫面 | 偵測到畫面 hash 大幅變動且不再是成員列表 → 暫停 + 提示 | ⏳ |
| 雷電 ADB port 不在預設範圍 | 用 ldconsole 反推（port = 5555 + 2×index），不用使用者手動 | ✅ |
| 中止掃描 | 二次確認 + 刪除 `data/captures/<session>/` 暫存資料夾 | ✅ |
| 改名誤觸 | 「確認改名」按下後跳 modal 二次確認 | ✅ |
| 結束統整誤關 | ✕/取消 偵測到 ❶ 區有填值時跳 confirm | ✅ |
| 防毒軟體誤判 ADB / PyInstaller exe | README 附上常見防毒軟體放行步驟（Defender、卡巴等） | ⏳ M8 |
| 使用者誤刪 Excel | 自動備份目錄 + GUI 提供「還原備份」按鈕 | 🚧（備份已有，按鈕未做）|
| 第一次使用沒有 Excel | 啟動時若 Excel 不存在 → 自動建立含正確標頭的新檔 | 🚧（load 寬容，但缺「自動建立空檔」流程）|
| 工作簿只有 correct_nickname 一欄 | load() fallback 到第一個 sheet；save 自動補齊所有欄位 | ✅ |

---

## 10. 開發里程碑（含已完成項目）

| 階段 | 目標 | 狀態 |
|------|------|------|
| **M1：ADB 與截圖** | ADB 連雷電、截圖、滑動、不受視窗大小影響 | ✅ 完成 |
| **M2：OCR PoC** | RapidOCR 對 RO 公會成員頁穩定辨識 | ✅ 完成（v5-server 證實可用）|
| **M3：自動滑動 + 去重** | 端到端跑完一次擷取（150 人）| ✅ 完成 |
| **M3.5：OCR 三方對照與選型** | v4-mobile / v5-mobile / v5-server | ✅ 完成 |
| **M4：Excel 寫入（v1）** | 寬表格、自動備份、peak / HH:MM 欄 | ✅ 完成 |
| **M4.5：Hybrid OCR + 管線並行** | v5-mobile 主 + v5-server fallback；producer/consumer thread；停用 cls | ✅ 完成（2026-05-19 起每次掃描都用）|
| **M5a：模糊比對 + 合併報告** | rapidfuzz 四階段、OpenCC t2s、三段式 A/B/C 報告、紅底列 | ✅ 完成（CLI 走 `bootstrap_excel.py --update`）|
| **M5b：改名/衝突解決對話框** | 改名 modal、phase-A exact + phase-B fuzzy、結束統整 review dialog | ✅ 完成（2026-05-21）|
| **M6：主 GUI（§8 狀態機）** | customtkinter，環境檢查、即時掃描列表、即時裝評差異著色、改名管理、sticky header、中止+清理、menu bar 重新掃描 | ✅ 完成（2026-05-21）|
| **M6.5：v1→v2 schema 遷移** | 一天一欄 + 取 max；移除 known_aliases / 貢獻 / 活躍；新欄名 `最高裝評` | ✅ 完成（`scripts/migrate_excel_v2.py`）|
| **M7：防呆與例外處理** | §9 全部情境覆蓋 + log 系統 | 🚧 部分（見 §9 表）|
| **M8：PyInstaller 打包與測試** | 乾淨 Windows 測試；A/V 白名單；icon；模型隨檔分發 | ⏳ 未開始 |
| **M9：使用手冊** | README + 首次設定教學圖 | ⏳ 未開始 |

目前可運作流程：

* **GUI（主流程）**：開啟雷電進到公會成員頁 → 雙擊 `scripts/run_gui.py` →
  按「重新偵測」確認 ADB / 模擬器 / Excel 都綠燈 → 按「開始掃描」 →
  即時看到每位成員的裝評變化（綠色 +xxx / 紅色 -xxx）→ 掃完自動 merge 入 Excel + 備份。
* **CLI（除錯用）**：手動進到公會成員頁 → `probe_capture.py` → `bootstrap_excel.py --update`。
* **遷移**：第一次升到 v2 schema 前先跑 `migrate_excel_v2.py`（會自動備份原檔）。

2026-05-19 實測一次完整 150 人公會：擷取 40 頁 / 149 unique / 513 s；
合併新增 2 人、漏抓 3 人、0 筆需 fuzzy review。

---

## 11. 打包策略

### 11.1 PyInstaller 設定要點

- `--onefile`：產生單一 `.exe`（啟動稍慢但傳輸方便）
- `--noconsole`：GUI 不顯示黑色 cmd 視窗
- `--icon=assets/icon.ico`
- `--add-data "models/*;models"`：把 OCR 模型一起包進去
- `--add-binary "bin/adb.exe;bin"`
- 版本資訊：用 `version_info.txt` 帶上版本號與作者，減少 Defender 誤判
- 程式碼簽章：（選配）若預算允許，做 Authenticode 簽章可大幅降低防毒誤判

### 11.2 預期 exe 大小

- 含 RapidOCR 模型 + ADB + Python runtime → 約 **120~180 MB**
- 對 Discord / Telegram 分享來說可接受

### 11.3 首次啟動體驗

第一次跑 `RO_GearSync.exe`：

1. 解壓縮到 `%TEMP%` → 約 3~5 秒
2. 自動建立 `./data/`、`./logs/`、`./data/backups/`
3. 自動建立空的 `guild_scores.xlsx`、`aliases.json`、`config.json`
4. 跳出「歡迎」對話框，引導：
   - 雷電開啟 ADB 偵錯的截圖步驟
   - 建議解析度設定（1280×720）
   - 把 exe 與 data/ 放在 Google Drive 同步資料夾的建議

---

## 12. 專案目錄結構（2026-05-20 實況）

```
RO_GearSync/
├── PLAN.md                          ← 本計畫書
├── pyproject.toml
├── requirements.txt
├── RO_GearSync.code-workspace
│
├── data/
│   ├── guild_scores.xlsx            ← 主工作簿（M4 產物）
│   ├── backups/                     ← 自動時間戳備份
│   └── captures/<YYYYMMDD_HHMMSS>/  ← 每次擷取的 session 資料
│       ├── page_*.png                  原始截圖
│       ├── members.json                dedup 後成員清單
│       └── summary.json                halt reason / pages / duration
│
├── logs/                            ← loguru 滾動日誌
│
├── scripts/                         ← CLI 進入點 / 開發工具
│   ├── probe_adb.py
│   ├── probe_ocr.py
│   ├── probe_parse.py
│   ├── probe_tune.py
│   ├── probe_capture.py
│   ├── reanalyze_session.py
│   ├── compare_ocr.py
│   ├── inspect_capture.py
│   ├── bootstrap_excel.py
│   ├── migrate_excel_v2.py          ← v1 → v2 schema 遷移
│   └── run_gui.py                   ← GUI 主入口
│
└── src/ro_gearsync/                 ← 套件本體
    ├── __init__.py                  ← __version__ 字串
    ├── adb/
    │   ├── __init__.py
    │   ├── client.py
    │   └── port_scanner.py
    ├── capture/
    │   ├── __init__.py
    │   └── session.py
    ├── vision/
    │   ├── __init__.py
    │   ├── layout.py
    │   ├── ocr.py
    │   └── parser.py
    ├── matching/
    │   ├── __init__.py
    │   └── matcher.py
    ├── storage/
    │   ├── __init__.py
    │   └── excel.py
    ├── gui/                         ← M6 主視窗
    │   ├── __init__.py
    │   ├── app.py                   ← RoGearSyncApp 主視窗 + 狀態機
    │   ├── scan_runner.py           ← worker thread + 事件 queue
    │   └── rename_dialog.py         ← 改名 modal
    └── utils/
        ├── __init__.py
        ├── logging.py
        └── paths.py
```

模板資料夾 (`assets/templates/`)、隨打包附帶的 `bin/adb.exe`、`models/`、`tests/` 都是 M8 才會出現的東西，目前還沒建立。

### 12.1 `src/ro_gearsync/` 各檔用途

#### `adb/` — 與雷電的對話層

- **`client.py`** — `AdbClient` thin wrapper：呼叫打包的 `adb.exe` 跑
  `connect / devices / shell screencap / shell input swipe / version`。
  每次呼叫都 spawn 一個短命子程序（避免 Windows 上 adb shell pipe 死等），
  代價是每呼叫多幾毫秒，換來不會卡住。
- **`port_scanner.py`** — `find_ldplayer_devices()`：依序 `try connect`
  `127.0.0.1:5555 / 5557 / … / 5585`（LDPlayer 9 的 instance 0–15），
  也把 `adb devices` 列出但沒 `connect` 過的 `emulator-NNNN` 一起回報。

#### `capture/` — 擷取迴圈

- **`session.py`** — `CaptureSession`，整個 producer / consumer pipeline 的核心。
  producer thread 跑 `screencap → 推 queue → swipe → settle`；
  consumer (主 thread) 跑 `OCR → row parse → 低信心 fallback re-OCR →
  dedup 合併 → 回呼 progress`。連 N 頁沒新行 (`halt=no_new_rows`) 或達
  `max_pages` 上限就退出，結束時把整段 session 寫入 `data/captures/<ts>/`。
  滑動長度/速度/落點 Y 有隨機抖動，降低被遊戲反作弊偵測為 bot 的機率
  （memo: 2026-05-18 觸發過軟性警告）。

#### `vision/` — 影像 → OCR → 結構化列

- **`layout.py`** — `GuildPageLayout`：把公會成員頁的 ROI 用「相對比例」表示
  （`rows_top=0.26 / rows_bottom=0.82` 等），與裝置實際解析度脫鉤。1920×1080
  量出來的數值可直接套用到 1280×720。`median()` 是給 parser 用的工具函式。
- **`ocr.py`** — `OcrEngine` 對 RapidOCR 的薄包裝。預載入較慢（~0.6s 暖／多秒冷），
  載入後 stateless。**寫死 `use_cls=False`** — cls 在這個遊戲花俏字型上會誤翻
  `9↔6` 等鏡像字、害 OCR 信心 1.00 給出錯字（詳見 §6.5）。輸入支援檔案路徑或
  numpy 陣列（後者給 ROI 重 OCR 用）。
- **`parser.py`** — `parse_member_page()` / `refine_nicknames()`：把整頁 OCR
  結果拆成一列一列。以「裝評數字欄」作 anchor，定一條 row band，再到暱稱欄抓最近的
  文字框；可選擇對暱稱 ROI 做 2x 上採樣再 OCR，邊緣案例信心常從 0.8 升到 0.95+。

#### `matching/` — 把 OCR 結果配上既有玩家

- **`matcher.py`** — `Matcher.match()` 對單一 OCR 暱稱回傳 `MatchResult`。
  - `normalize_for_match()`：NFKC → 去裝飾標點 → casefold → OpenCC 繁→簡
    （吸收 OCR 偶爾吐簡體字、玩家暱稱混繁簡的情況）。
  - 三檔閾值常數：`MATCH_AUTO_HIGH=95` / `MATCH_AUTO_LOW=80` / `MATCH_REVIEW=65`，
    依此判定 `AUTO_HIGH / AUTO_LOW / REVIEW / NEW`。
  - 候選池涵蓋 `correct_nickname`、`latest_ocr_nickname`、`known_aliases`；
    分數取 `max(ratio, partial_ratio, token_set_ratio)`。同次擷取裡用
    `claimed` set 防止兩個 OCR 列搶同一個 record。

#### `storage/` — Excel 持久化

- **`excel.py`** — `GuildScoresWorkbook`：6 個 meta 欄
  (`correct_nickname` / `latest_ocr_nickname` / `confidence` / `known_aliases` /
  `review_reason` / `peak_gear_score`) + 每次擷取追加 3 個欄
  (`裝評 / 貢獻 / 活躍 YYYY-MM-DD HH:MM`)。
  - `bootstrap_from_captures()`：第一次建立工作簿。
  - `merge_capture()`：四階段配對 (`exact_correct → exact_ocr → fuzzy_correct →
    fuzzy_ocr → new`)，回傳 `MergeResult`（包含每筆 `MergeMatch` 與
    missed/unmatched 清單）。**`correct_nickname` 欄唯讀**、既有列順序與
    既有列數永遠不動（鐵則）。
  - `save(backup=True)`：寫前自動備份到 `data/backups/`，名稱含時間戳。
  - `review_reason=new_member` / `missed_this_capture` 的列整列染紅，
    其餘 review reason 只染 `review_reason` 那個 cell。

#### `utils/` — 共用小工具

- **`logging.py`** — loguru 設定：標準錯誤輸出走 INFO；同時寫滾動檔到 `logs/`。
  整個套件用 `from ro_gearsync.utils.logging import logger` 拿同一個 logger。
- **`paths.py`** — 處理「直接跑 source」vs「PyInstaller --onefile 打包」的路徑差異。
  `bundle_dir()` 在打包後指 `sys._MEIPASS`、在開發模式指專案根目錄。
  另外暴露 `adb_binary()` / `user_data_dir()` / `logs_dir()` 等。

### 12.2 `scripts/` 各檔用途（CLI 工具，都用 `.venv\Scripts\python.exe scripts\<name>.py` 跑）

| 腳本 | 對應里程碑 | 用途 |
|---|---|---|
| `probe_adb.py` | M1 | 列出當前看到的 adb binary、掃 LDPlayer port、讀螢幕尺寸、拍 1 張截圖；`--swipe` 測試上滑。**首次設定機器時用。** |
| `probe_ocr.py` | M2 | 把單張截圖整張塞給 RapidOCR，列出所有文字框並輸出標註圖。視覺驗證 OCR 有沒有抓到行。 |
| `probe_parse.py` | M3 | 端到端跑單張截圖：行切分 + 暱稱 ROI 重 OCR；輸出帶編號/綠/橙/紅框的標註圖，加 primary vs refined 文字對照。 |
| `probe_tune.py` | M3 後續調參 | 掃 RapidOCR 偵測器 `box_thresh` / `unclip_ratio` 等參數，找哪一組能撈回被丟掉的小字（例如「-南湘楚-」的破折號）。 |
| `probe_capture.py` | M3 / M4.5 | **常用入口**。完整擷取流程：連 LDPlayer → 預翻到頂 → producer/consumer 跑滿 → 寫 `data/captures/<ts>/`。預設 `v5-mobile` 主 + `v5-server` fallback (threshold 0.80)。結束自動 `adb disconnect`。 |
| `reanalyze_session.py` | M3.5 | 拿既有 session 的截圖重跑 OCR (`--quality v5-server` 等)，產生 `members_<quality>.json`，不必再翻雷電。 |
| `compare_ocr.py` | M3.5 | 在同一個 session 內把所有 `members*.json` 變體並排比較（用 gear score 當 key），找出哪些列 OCR 結果有分歧。 |
| `inspect_capture.py` | M3 後 QA | 給定 session，回報總筆數、缺暱稱列、裝評數值衝突、低信心列、候選變體。供人眼最後一次過濾。 |
| `bootstrap_excel.py` | M4 / M5a | 把 capture session 的 `members.json` 寫進主工作簿。`--update` 走 §7.1 四階段合併並輸出 A/B/C 三段報告；無 `--update` 是第一次建檔 (bootstrap) 模式；`--dry-run` 只跑記憶體不存檔。 |
| `migrate_excel_v2.py` | M6.5 | 把 v1 schema 的工作簿轉成 v2（每天一欄取 max、移除 known_aliases / 貢獻 / 活躍）。`--dry-run` 預覽不寫檔；動手前一定備份原檔。 |
| `run_gui.py` | M6 | **主使用者入口**。啟動 customtkinter 視窗。 |

> 一般使用 = 雙擊 `run_gui.py`，看 UI 完成所有事情。CLI (`probe_capture.py` + `bootstrap_excel.py --update`) 留作除錯與重跑用；其餘 probe / inspect / compare 系列是調參、QA、A/B 測試用的開發者工具，正式打包進 exe 時可不收。

---

## 13. 風險與限制

| 風險 | 評估 | 緩解 |
|------|------|------|
| **遊戲版本更新導致 UI 改版** | 中高 | 模板匹配採用「多個模板 + 容忍度高」策略；提供更新模板的 GUI |
| **OCR 對某些藝術字暱稱辨識率差** | 中 | 提供「永遠加入別名清單」功能，使用者修一次就好 |
| **遊戲反作弊偵測 ADB**（已實證） | **中** — 2026-05-18 實際觸發過一次「偵測到非法行為，請重啟」軟性警告 | (1) 滑動時長 / 距離 / 間隔 / 終點 Y 隨機化，避免一模一樣的節奏 (2) 每 N 頁插入隨機長暫停 (3) 掃描結束自動 `adb disconnect` (4) README 明確告知這個風險、建議「同一帳號一週不要掃太多次」「掃前關掉同一遊戲家族的其他客戶端」 |
| **防毒軟體把 exe 標記為可疑** | 高（PyInstaller 通病） | 提供 SHA256、放上 GitHub Release、附 Defender 白名單教學 |
| **使用者改動 Excel 結構** | 中 | 啟動時驗證表頭，不符合就提示並備份原檔 |

---

## 14. 後續可選的擴充功能（不在 v1 範圍）

- Discord Webhook：擷取完成後自動發摘要到公會頻道
- 圖表頁面：個人裝評趨勢線圖、公會整體分布直方圖
- 多公會支援：一個 .exe 管理多個 Excel
- 自動排程：每週五晚上自動執行一次（會需要保留登入狀態，複雜度較高）
- 雲端同步：v1 用 Google Drive 同步資料夾即可達成，後續可整合 Google Sheets API（如未來需要）

---

## 15. 下一步建議

1. **先做 M1 + M2 的原型驗證**（約 2~3 天）：證實 ADB 截圖與 OCR 辨識率可達實用水準。
2. 若原型 OCR 對暱稱辨識率穩定 ≥ 90%、對裝評辨識率 ≥ 98%，就可以放心進入 M3 之後。
3. 若辨識率不夠，再評估：
   - 是否需要微調 OCR 模型（用我們自己收集的 RO 截圖做 fine-tune）
   - 是否切換到 PaddleOCR 完整版（換來更高辨識率，但 exe 大三倍）
4. 視驗證結果，再請您提供 5~10 張公會成員頁的真實截圖（遮掉敏感資料即可）做為測試 fixture。

---

*文件版本：v2.1 — 2026-05-21（同步 M5b + M6 完成；改名/重新掃描/結束統整對話框全部上線）*

---

## 16. 未完成功能盤點（2026-05-21 視角）

依「需求對照 vs. 實際代碼」做一次盤點。下表把 PLAN 寫過、需求文件提過、但目前 GUI / CLI 沒實作的東西列清楚，讓下一輪改動好抓。

### 16.1 GUI 互動

| 項目 | 出處 | 現況 |
|---|---|---|
| 畫面不是成員列表的偵測 | §9 | ✅ 第一頁 sanity check + messagebox 引導（紅框版被使用者撤掉，純文字夠用）|
| 擷取中途切換畫面偵測（hash diff） | §9、§5 | 未做。目前只依賴使用者「掃描中請勿操作雷電」自律 |
| 「還原備份」按鈕 | §9 | 備份已自動產生於 `data/backups/`，但 GUI 沒按鈕；使用者得手動到資料夾複製 |
| 首次啟動精靈 / 引導 | §11.3 | 未做。`run_gui.py` 直接開主視窗（但 config.ini 第一次啟動會自動產生並帶註解，降低門檻）|
| 雷電輸入鎖定（鎖定 KB/mouse） | §8.5 技術層 | 未做。目前只有文字提示「請勿操作雷電」 |
| 改名登記預檢對話框（§8.3） | §8 | **不會做**。改名 modal 隨時可叫，預檢的價值消失 |
| 結束後 MISSED/UNMATCHED 互動推薦（§8.4） | §8 | ❷ 區現在有原始截圖縮圖讓使用者目視確認；但還沒「自動推薦這個 OCR 是誰的改名」rapidfuzz 推薦下拉 |

### 16.2 後台 / 工程

| 項目 | 出處 | 現況 |
|---|---|---|
| PyInstaller 打包 (`.exe`) | M8 / §11 | 完全未做。所有相關設定（`--add-data models`、`--icon`、版本資訊）都還沒寫 |
| 打包用的 `bin/adb.exe` | §3.3 | 目前都用 LDPlayer 自己的 adb.exe；要打包獨立 exe 才需要 |
| `models/` 目錄打包 | §11.1 | RapidOCR 模型目前從 cache 載入；打包時要把 ONNX 模型一起包 |
| README + 教學圖 | M9 / §11.3 | 完全未做 |
| 自動建立空 Excel | §9 | 部分：load() 對缺檔很寬容，但首次完整 bootstrap 還沒有 GUI 流程 |
| Tests | — | 沒有 pytest 套件；目前所有驗證都是手動 smoke test |

### 16.3 已從規劃中拿掉的功能

清單在這裡，提醒自己「這些不要再加回來」：

- `aliases.json` 別名字典（§4.2 已說明）— 由 `latest_ocr_nickname` + fuzzy 取代
- `known_aliases` 欄位 — M6.5 schema 遷移時移除
- 貢獻 / 活躍欄位 — M6.5 schema 遷移時移除（OCR parser 還會抓但儲存層丟掉）
- 同一天彈窗詢問「覆蓋 / 另存 / 取消」— v2 schema 改成同日自動取 max，不需要詢問
- `config.json` 設定檔 — 目前一切從 CLI argparse / GUI state 來；ADB port 自動偵測就足夠

### 16.4 已知技術債

- `scan_runner.py` 的 `_dispatch_exact` 在主執行緒 patch `session._merge` — 雖然 thread-safe（producer 還沒啟動），但 monkeypatch 風格脆弱。長期應該讓 `CaptureSession` 暴露正式的 hook。
- 重新掃描所有圖片」會把 `members_reocr.json` 留在 session 資料夾旁，但沒清理機制；多版本累積後使用者得手動刪。
- ADB / ldconsole 偵測在「重新偵測」期間會 spawn 多個 subprocess，沒做併發保護 — 連續快點兩次按鈕可能會疊起來（目前用 `_detecting` flag 擋）。

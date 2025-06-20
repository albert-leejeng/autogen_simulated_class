# Uedu 優學院 - 虛擬教室模擬器 (Virtual Classroom Simulator)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

這是一個基於 Microsoft AutoGen 框架開發的多智慧體（Multi-Agent）模擬專案。它的核心目標是為教育工作者提供一個強大的工具，用以在實際授課前，對教案（Lesson Plan）進行模擬與壓力測試。

透過為數十位虛擬學生（AI Agents）注入獨特的「數位孿生」人格特質與學習背景，本專案能夠模擬出真實課堂中的分組討論情境，並對教案的可行性、學生的可能反應、以及討論的最終成果提供富有洞見的預測。

## 專案功用與核心價值

在教學設計中，傳統教案評估往往依賴教師的個人經驗。本專案旨在將此過程數據化、模擬化，為教師帶來以下核心價值：

*   **預測性洞察**：在實際教學前，預測不同學生群體對特定教案（如技術實作、創意發想、思辨辯論）的反應與投入程度[2]。
*   **教案壓力測試**：觀察教案在面對不同學習風格與個性的學生[3]（如領導者、沉默者、批判者）時，能否有效引導討論並達成教學目標。
*   **數據驅動的教學優化**：所有模擬過程（對話、評論、總結）都會被完整記錄在資料庫中，為教學研究與教案迭代提供寶貴的數據支持。
*   **節省時間與資源**：透過模擬快速試錯，教師可以更有效率地調整教學策略，將時間投入在最優化的教學方案上。

---

## 核心功能

*   **動態多智慧體模擬**：利用 AutoGen 創建多達 30 位具有獨特個性的 AI 學生代理人。
*   **數位孿生人格**：每位學生的人格、學習背景和互動風格均由 `simulated_students.json` 定義，實現高度客製化的模擬。
*   **智慧共識終止機制**：討論不會因固定訊息次數而結束，而是由一個獨立的 AI 評估小組是否已達成「共識」，使討論過程更自然。
*   **多層次評估流程**：
    1.  **小組內部討論**：學生們分組進行討論。
    2.  **教師逐組評論**：討論結束後，一個 AI 教師代理人會針對該組的討論紀錄生成評論。
    3.  **教案最終評估**：在所有小組都完成後，一個 AI 評估專家會總結所有老師的評論，對原始教案的可行性提出最終報告。
*   **永續化數據紀錄**：所有對話、評論和評估報告都會即時存入 MySQL 資料庫，便於後續分析。

---

## 快速上手 (Quick Start)

請依照以下步驟來設定並執行你的第一個虛擬教室模擬。

### 1. 環境需求

*   Python 3.10 或更高版本
*   一個可用的 MySQL 資料庫服務

### 2. 安裝步驟

**a. 克隆專案**
```
git clone <你的專案Git儲存庫URL>
cd <專案名稱>
```

**b. 安裝 Python 依賴套件**

專案所需的套件已列於 `requirements.txt`。

```
pip install -r requirements.txt
```
如果 `requirements.txt` 不存在，請手動建立並填入以下內容：
```
autogen-agentchat[openai,websockets]
aiomysql
python-dotenv
```
然後再次執行 `pip install` 指令。

**c. 設定環境變數**

這是最重要的一步。專案透過環境變數來讀取你的 API 金鑰和資料庫連線資訊[7][11]。

1.  在專案根目錄下，複製 ` .env.example` 並將其重新命名為 `.env`。
2.  打開 `.env` 檔案，填入你的個人設定：

```
# .env

# OpenAI API Key (必要)
# 請至 https://platform.openai.com/api-keys 取得
OPENAI_API_KEY="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# MySQL 資料庫連線設定
MYSQL_HOST="127.0.0.1"
MYSQL_PORT="3306"
MYSQL_USER="root"
MYSQL_PASSWORD="your_database_password"  # 請替換成你的資料庫密碼
MYSQL_DB="classroom_discussion"
```

**d. 準備資料檔案**

請確保專案根目錄下有以下兩個 JSON 檔案：
*   `simulated_students.json`：定義所有學生的數位孿生人格。
*   `lesson_plans.json`：定義可供選擇的教案與其初始提問。

專案中應已包含範例檔案[2][3]，你可以直接使用或根據需求修改。

### 3. 執行模擬

一切就緒後，在終端機中執行主程式：

```
python main.py
```

程式啟動後，會先檢查並建立所需的資料庫表格。接著，它會列出 `lesson_plans.json` 中所有可用的教案，並提示你輸入編號來選擇要模擬的項目。

選擇後，模擬便會開始，你會在終端機中即時看到各小組的討論過程、共識檢查，以及老師的評論。

---

## 專案架構

```
/
├── main.py                   # 主執行腳本
├── simulated_students.json   # 學生數位孿生設定檔
├── lesson_plans.json         # 教案設定檔
├── requirements.txt          # Python 依賴套件
├── .env                      # (私有) 環境變數檔案
└── README.md                 # 本說明文件
```

### 資料庫結構

程式首次執行時，會自動在你的 MySQL 資料庫中建立以下四個表格[8]：

*   `discussion_groups`：紀錄每個討論小組的資訊。
*   `messages`：儲存每一位學生的每一句發言，並關聯到對應的小組。
*   `teacher_comments`：儲存 AI 教師對每個小組討論的評論。
*   `final_evaluations`：儲存 AI 評估專家對整個教案的最終可行性報告。

---

## 如何客製化

*   **修改學生人格**：直接編輯 `simulated_students.json`，調整學生的 `llm_persona_prompt` 即可改變其行為模式。
*   **新增教案**：在 `lesson_plans.json` 中依現有格式新增一個新的 JSON 物件，即可在程式啟動時看到新的選項。
*   **調整討論行為**：修改 `main.py` 中的 `ConsensusTermination` 類別，可以調整共識判斷的敏感度或檢查頻率。

## 授權 (License)

本專案採用 MIT 授權。詳情請見 LICENSE 檔案。
```

目前的改善方向:
1.現在是所有組別討論同一問題，能否在前一組的討論結果為基礎，進行更進一步的討論呢?

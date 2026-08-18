# SugarTopia Backend 開啟流程筆記

這份筆記記錄目前後端的啟動方式、環境變數設定，以及今天遇到的錯誤原因。下次重新開專案時，可以照著這份文件走。

## 這個後端在做什麼

這是一個 FastAPI 後端，主要功能是提供聊天 API：

```text
POST /api/chat
```

程式啟動時會做幾件事：

1. 讀取 `.env` 裡的 Gemini API key。
2. 讀取 `dessert_data_sample.json` 裡的甜點資料。
3. 用 Gemini embedding model 把甜點資料轉成向量。
4. 用 Chroma 建立可搜尋的向量資料庫。
5. 啟動 `/api/chat`，讓使用者可以問甜點推薦問題。

## 目前使用的後端框架

這個專案目前已經有後端框架，使用的是：

```text
FastAPI
```

可以把它理解成 Python 後端世界裡負責建立 API 的框架。

如果用前端 Vue 來對照：

```text
Vue
```

負責畫面、按鈕、輸入框、聊天視窗、使用者互動。

```text
FastAPI
```

負責 API 路由、接收前端傳來的 request、處理資料、呼叫 Gemini，最後回傳 response 給前端。

目前後端入口在 `main.py`：

```python
app = FastAPI()
```

這行代表程式建立了一個 FastAPI app。

目前主要 API 在 `main.py`：

```python
@app.post("/api/chat")
def chat_with_gemini(request: ChatRequest):
```

這代表後端有一個聊天 API：

```text
POST /api/chat
```

Vue 前端之後可以用 `fetch` 或 `axios` 呼叫：

```text
http://127.0.0.1:8000/api/chat
```

送出的資料格式是：

```json
{
  "message": "我想吃抹茶甜點"
}
```

後端回傳的格式大概是：

```json
{
  "reply": "推薦內容..."
}
```

簡單說：

```text
Vue 畫畫面，FastAPI 提供資料和 AI 回答。
```

目前不需要再另外加一個後端框架。等專案變大之後，才會考慮把 `main.py` 拆成 `routers/`、`services/`、`schemas/` 等資料夾。

## Laravel 和 FastAPI 是同一類東西嗎？

是，可以先把它們理解成同一大類：

```text
後端 Web 框架
```

差別主要是使用的程式語言和生態系：

```text
Laravel = PHP 的後端框架
FastAPI = Python 的後端框架
```

它們都可以做這些事：

1. 建立 API 路由。
2. 接收前端送來的 request。
3. 驗證資料格式。
4. 處理後端邏輯。
5. 連接資料庫或外部服務。
6. 回傳 JSON response 給前端。

用前後端分工來看：

```text
前端 HTML/CSS/JavaScript 或 Vue
  ↓ 呼叫 API
後端 Laravel 或 FastAPI
  ↓ 查資料、處理邏輯、呼叫 Gemini
回傳 JSON 給前端
```

所以公司如果是 Vue + Laravel，這個專案目前是靜態前端 + FastAPI。兩者的角色很像，只是 Laravel 用 PHP，FastAPI 用 Python。

Laravel 通常比較像完整全家桶，常見功能包含 MVC、ORM、migration、會員登入、權限、queue 等。FastAPI 比較輕量，很適合做 API，也很常被拿來接 Python 的 AI、資料處理、機器學習套件。

## FastAPI 文件頁 `/docs` 是什麼？

你在瀏覽器看到的這個網址：

```text
http://127.0.0.1:8000/docs
```

不是前端網站，而是 FastAPI 自動產生的 API 文件和測試平台。

它的用途是：

1. 顯示目前後端有哪些 API。
2. 告訴你每支 API 要用 `GET` 還是 `POST`。
3. 告訴你 API 網址，例如 `/api/chat`。
4. 顯示前端要送什麼資料格式。
5. 讓你不用寫前端，也可以直接在瀏覽器測試 API。

畫面上幾個部分可以這樣看：

```text
FastAPI
```

代表這是 FastAPI 自動產生的文件頁。

```text
0.1.0
```

API 文件的版本號，目前只是預設值，不是很重要。

```text
OAS 3.1
```

代表這份 API 文件符合 OpenAPI Specification 3.1。OpenAPI 是一種描述 API 規格的標準。

```text
/openapi.json
```

這是機器可讀的 API 規格資料。一般開發時不太需要手動打開，但有些工具可以讀它來產生 API 文件或前端型別。

```text
GET /
```

首頁 API。現在打開 `http://127.0.0.1:8000/` 會回傳後端正在運作的簡單訊息。

```text
GET /health
```

健康檢查 API。用來確認後端和 AI 服務是否初始化成功。

```text
POST /api/chat
```

目前最重要的聊天 API。前端要把使用者輸入的文字送到這裡，後端會搜尋甜點資料、組 prompt、呼叫 Gemini，最後回傳回答。

```text
Schemas
```

資料格式說明。例如 `ChatRequest` 代表 `/api/chat` 需要收到的 request body 格式。

## 目前整體開發清單

這是目前前後端加 AI 串接的大方向：

1. 建置後端基礎環境：建立 Python FastAPI 專案，設定 CORS，成功啟動本地後端。
2. 打通 Gemini API 連線：在 `.env` 設定 API key，確認後端可以呼叫 Gemini。
3. 實作 RAG 資料處理：讀取 `dessert_data_sample.json`，把甜點資料轉成向量，存進 Chroma 向量資料庫。
4. 建立問答 API：建立 `POST /api/chat`，接收前端問題，搜尋相關甜點資料，組成 prompt 給 Gemini，回傳推薦結果。
5. 前端介面串接：在 SugarTopia 前端加入輸入框和對話區，用 `fetch` 或 `axios` 呼叫 `/api/chat`，把回傳文字顯示在畫面上。

目前進度：

```text
後端 FastAPI 已啟動
Gemini API key 已改成從 .env 讀取
RAG 基礎流程已建立
POST /api/chat 已建立
下一步是把前端畫面接到 POST /api/chat
```

## 靜態前端會不會比較難串後端？

你的前端在這個資料夾：

```text
/Users/mike/Documents/emily_project_archive/SugarTopia
```

目前看起來是早期學前端時做的靜態網站，主要是：

```text
HTML
CSS
JavaScript
```

不是 Vue 專案，也沒有看到像 `package.json`、`vite.config.js`、`src/` 這種 Vue/Vite 專案常見結構。

這樣不會讓串接變得很困難。只要是瀏覽器裡的 JavaScript，就可以用 `fetch` 呼叫後端 API。

靜態前端串後端的概念是：

```text
使用者在 HTML 頁面輸入問題
  ↓
JavaScript 用 fetch() 送到 FastAPI
  ↓
FastAPI 呼叫 Gemini 和甜點資料
  ↓
回傳 JSON
  ↓
JavaScript 把 reply 顯示到畫面上
```

之後前端大概會加這種 JavaScript：

```javascript
async function askGemini(message) {
  const response = await fetch("http://127.0.0.1:8000/api/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      message: message
    })
  });

  const data = await response.json();
  return data.reply;
}
```

如果你之後想改成 Vue，也可以，但不是現在必須做的事。

建議順序是：

1. 先用目前的靜態 HTML/CSS/JS 把 `/api/chat` 串起來。
2. 確認 AI 問答流程可以跑。
3. 再決定要不要重構成 Vue 或 Nuxt。

原因是：現在最重要的是先證明「前端可以問問題，後端可以回 AI 推薦」。等功能跑通，再換前端框架會比較安心。

## 前端已經怎麼接到 `/api/chat`

目前你的前端首頁是：

```text
/Users/mike/Documents/emily_project_archive/SugarTopia/index.html
```

後端聊天 API 是：

```text
POST http://127.0.0.1:8000/api/chat
```

串接的意思就是：

```text
前端輸入框拿到使用者文字
  ↓
用 fetch() 把文字送到後端 /api/chat
  ↓
後端回傳 JSON
  ↓
前端把 data.reply 顯示在畫面上
```

目前已經直接改在前端專案的 `master` 分支，修改了這些檔案：

```text
/Users/mike/Documents/emily_project_archive/SugarTopia/index.html
/Users/mike/Documents/emily_project_archive/SugarTopia/CSS/style.css
/Users/mike/Documents/emily_project_archive/SugarTopia/Js/gemini-chat.js
```

### 第 1 步：確認後端有開

先在後端資料夾啟動：

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_backend
source venv/bin/activate
uvicorn main:app --reload
```

看到這些代表後端成功：

```text
✅ 甜點資料庫載入成功！
Application startup complete.
```

### 第 2 步：在前端 HTML 加聊天區

已經在 `index.html` 加入 AI 聊天區。原理是放一個輸入框、送出按鈕、以及顯示 Gemini 回覆的訊息區。

加入的結構類似：

```html
<section class="ai-chat-section">
  <div class="ai-chat-container">
    <h2>Ask SugarTopia AI</h2>
    <p>Tell me what kind of dessert you want today.</p>

    <div id="chatMessages" class="chat-messages"></div>

    <form id="chatForm" class="chat-form">
      <input
        id="chatInput"
        type="text"
        placeholder="例如：我想吃抹茶甜點，有推薦嗎？"
        autocomplete="off"
      />
      <button type="submit">Send</button>
    </form>
  </div>
</section>
```

### 第 3 步：載入 JavaScript

已經在 `index.html` 載入：

```html
<script src="Js/gemini-chat.js" defer></script>
```

### 第 4 步：新增 `Js/gemini-chat.js`

已經新增這個檔案：

```text
/Users/mike/Documents/emily_project_archive/SugarTopia/Js/gemini-chat.js
```

內容放：

```javascript
const chatForm = document.querySelector("#chatForm");
const chatInput = document.querySelector("#chatInput");
const chatMessages = document.querySelector("#chatMessages");

function addMessage(role, text) {
  const message = document.createElement("div");
  message.className = `chat-message ${role}`;
  message.textContent = text;
  chatMessages.appendChild(message);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

chatForm.addEventListener("submit", async function (event) {
  event.preventDefault();

  const message = chatInput.value.trim();

  if (!message) {
    return;
  }

  addMessage("user", message);
  chatInput.value = "";
  addMessage("assistant", "思考中...");

  try {
    const response = await fetch("http://127.0.0.1:8000/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        message: message
      })
    });

    const data = await response.json();
    const loadingMessage = chatMessages.lastElementChild;

    if (!response.ok) {
      loadingMessage.textContent = data.detail || "後端發生錯誤，請稍後再試。";
      return;
    }

    loadingMessage.textContent = data.reply || data.error || "沒有收到回覆。";
  } catch (error) {
    const loadingMessage = chatMessages.lastElementChild;
    loadingMessage.textContent = "連不上後端，請確認 FastAPI 是否正在執行。";
  }
});
```

### 第 5 步：加一點 CSS

可以加在：

```text
/Users/mike/Documents/emily_project_archive/SugarTopia/CSS/style.css
```

最下面加入：

```css
.ai-chat-section {
  padding: 64px 20px;
  background: #fff8ef;
}

.ai-chat-container {
  max-width: 760px;
  margin: 0 auto;
}

.ai-chat-container h2 {
  margin-bottom: 8px;
  color: #3a2513;
}

.ai-chat-container p {
  margin-bottom: 20px;
  color: #6f5b49;
}

.chat-messages {
  min-height: 220px;
  max-height: 360px;
  overflow-y: auto;
  padding: 16px;
  border: 1px solid #ead7bd;
  background: #ffffff;
}

.chat-message {
  margin-bottom: 12px;
  padding: 10px 12px;
  line-height: 1.6;
}

.chat-message.user {
  margin-left: auto;
  max-width: 80%;
  background: #f9a726;
  color: #ffffff;
}

.chat-message.assistant {
  margin-right: auto;
  max-width: 80%;
  background: #f5eadc;
  color: #3a2513;
}

.chat-form {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.chat-form input {
  flex: 1;
  padding: 12px;
  border: 1px solid #ead7bd;
}

.chat-form button {
  padding: 12px 20px;
  border: 0;
  background: #3a2513;
  color: #ffffff;
  cursor: pointer;
}
```

### 第 6 步：打開前端測試

如果你是直接用瀏覽器打開 `index.html`，也可以測。

如果用 VS Code Live Server，前端網址可能會像：

```text
http://127.0.0.1:5500/index.html
```

只要後端同時在：

```text
http://127.0.0.1:8000
```

就可以串起來。

如果送出問題後看到「連不上後端」，通常代表：

1. FastAPI 後端沒有開。
2. 後端不是跑在 `8000` port。
3. API 網址打錯。

## 專案重要檔案

```text
main.py
```

後端主程式，FastAPI、Gemini、Chroma 都在這裡設定。

```text
dessert_data_sample.json
```

甜點資料來源。

```text
.env
```

放自己的機密設定，例如 Gemini API key。這個檔案不要上傳，也不要貼給別人。

```text
.env.example
```

範例設定檔。給自己或別人看需要哪些環境變數，但裡面不放真的 key。

## 第一次設定 API key

如果還沒有 `.env`，先在專案資料夾執行：

```bash
cp .env.example .env
```

然後打開 `.env`，把內容改成自己的新 key：

```env
GOOGLE_API_KEY=你的Gemini_API_key
```

注意：不要把 API key 放進 `main.py`。

`main.py` 已經有這段程式，會自動從 `.env` 讀 key：

```python
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
```

這兩行不是終端機指令，不需要手動執行。

## 每次重新開啟後端

先進入專案資料夾：

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_backend
```

啟動 Python 虛擬環境：

```bash
source venv/bin/activate
```

啟動後端：

```bash
uvicorn main:app --reload
```

看到下面這幾行就代表成功：

```text
✅ 甜點資料庫載入成功！
Uvicorn running on http://127.0.0.1:8000
Application startup complete.
```

後端網址是：

```text
http://127.0.0.1:8000
```

## 如何停止後端

在正在跑 `uvicorn` 的終端機按：

```text
Ctrl + C
```

如果改了 `.env`，通常需要停止後端再重新啟動，新的設定才會生效。

## 測試聊天 API

後端啟動後，可以用這個指令測試：

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"我想吃抹茶甜點，有推薦嗎？"}'
```

如果成功，會回傳類似：

```json
{
  "reply": "..."
}
```

也可以打開 FastAPI 自動產生的 API 文件頁：

```text
http://127.0.0.1:8000/docs
```

這個頁面可以直接在瀏覽器裡測試 `/api/chat`。

## 今天遇到的錯誤紀錄

### 1. API key not valid

錯誤內容：

```text
API key not valid. Please pass a valid API key.
```

原因：

`main.py` 原本寫死了一組舊的 Google API key，而且那組 key 已經無效。

處理方式：

1. 不再把 key 寫在 `main.py`。
2. 改成從 `.env` 讀取 `GOOGLE_API_KEY`。
3. 重新產生新的 Gemini API key。
4. 把新 key 放進 `.env`。

### 2. text-embedding-004 is not found

錯誤內容：

```text
models/text-embedding-004 is not found
```

原因：

原本使用的 embedding model：

```text
models/text-embedding-004
```

已經不適合目前的 Gemini API 版本。

處理方式：

已改成：

```text
models/gemini-embedding-001
```

這個設定在 `main.py`：

```python
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")
```

如果以後想改 model，可以在 `.env` 加：

```env
GEMINI_EMBEDDING_MODEL=models/gemini-embedding-001
```

## 常見問題

### 我需要每次都 `cp .env.example .env` 嗎？

不用。

只有第一次沒有 `.env` 時才需要。如果已經把 key 填進 `.env`，又執行：

```bash
cp .env.example .env
```

會把原本的 `.env` 覆蓋掉，key 可能會消失。

### `.env` 裡面的 key 要不要加引號？

通常不用：

```env
GOOGLE_API_KEY=AIzaSy...
```

### `load_dotenv()` 要不要在終端機執行？

不用。

它是 Python 程式碼，已經寫在 `main.py` 裡，後端啟動時會自動執行。

### 看到 `(venv)` 是什麼意思？

代表你已經進入 Python 虛擬環境。

例如：

```text
(venv) ➜ SugarTopia_backend
```

這表示接下來執行的 Python / uvicorn 會使用這個專案自己的套件。

## 最短重啟版

如果 `.env` 已經設定好，下次只要：

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_backend
source venv/bin/activate
uvicorn main:app --reload
```

停止時按：

```text
Ctrl + C
```

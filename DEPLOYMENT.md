# SugarTopia 部署指南

這份文件只放「怎麼跑起來、怎麼部署、怎麼設定環境」這類操作型內容。概念解釋、踩雷紀錄、SQL／CSS／API 設計這些學習筆記都在 `BACKEND_LEARNING_NOTES.md`，不要混在這裡——這是這份文件存在的原因：之前部署步驟跟學習筆記全部寫在同一份檔案裡，越找越亂。

## 線上網址

前端（Vercel，git push 到 main 就會自動部署，不用手動下指令）：

```text
https://sugartopia.vercel.app
```

後端（Google Cloud Run）：

```text
https://sugartopia-backend-673387630043.asia-east1.run.app
```

後端 API 文件（Swagger UI，可以直接在瀏覽器裡測試每一支 API）：

```text
https://sugartopia-backend-673387630043.asia-east1.run.app/docs
```

Admin 店家收錄工具（沒有掛在任何導覽列上，只能直接開網址進去，需要登入白名單帳號）：

```text
https://sugartopia.vercel.app/admin/places
```

## 本機開發

### 後端

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_backend
source venv/bin/activate
uvicorn main:app --reload
```

看到這幾行代表成功：

```text
✅ SugarTopia 會員資料庫準備完成！
✅ 甜點資料庫載入成功！
Uvicorn running on http://127.0.0.1:8000
```

本機後端網址：`http://127.0.0.1:8000`。停止後端：在跑 `uvicorn` 的終端機按 `Ctrl+C`。

**注意**：本機後端跟正式環境現在共用同一個 Supabase 資料庫（見下面「資料庫」），本機測試寫的帳號/收藏/評論會真的進到正式資料庫，不是隔離的假資料。

**改了 `.env` 之後**：`uvicorn --reload` 只會自動重載程式碼變更，不會重新讀 `.env`，改完環境變數要整個停掉重開（`Ctrl+C` 再重新執行 `uvicorn main:app --reload`）。

### 前端（SugarTopia_nuxt）

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_nuxt
npm run dev
```

本機前端網址：`http://localhost:4000`（`package.json` 的 `dev` script 寫死 port 4000，不是 Nuxt 預設的 3000——3000 跟開發者另一個工作專案衝突）。預設會打正式環境的後端（`nuxt.config.ts` 的 `apiBaseUrl`）；想打本機後端的話，`.env` 設 `NUXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000`。

## 環境變數

本機開發用 `.env`（第一次設定：`cp .env.example .env`，再填自己的值，`.env` 不會被 git 追蹤）。Cloud Run 用服務本身的環境變數（見下面「更新 Cloud Run 環境變數」），跟 `.env` 完全分開，改一邊不會影響另一邊。

| 變數 | 用途 | 必填 |
|---|---|---|
| `GOOGLE_API_KEY`（或 `GEMINI_API_KEY`） | Gemini API key，AI 問答用 | 必填 |
| `GEMINI_MODEL` | Gemini 模型名稱 | 選填，預設 `gemini-3.6-flash` |
| `GEMINI_EMBEDDING_MODEL` | 向量化模型名稱 | 選填，預設 `models/gemini-embedding-001` |
| `GOOGLE_PLACES_API_KEY` | Google Maps Platform key，跟 Gemini 的 key 分開申請、分開計費，admin 收錄工具用 | 必填（沒有的話 admin 收錄相關 API 會回 503） |
| `DATABASE_URL` | Supabase PostgreSQL 連線字串 | 必填 |
| `ADMIN_EMAILS` | 逗號分隔的 email 白名單，只有這些帳號能用 admin 收錄工具（搜尋 Google Places、新增店家）。沒設定 = 全部擋掉，不是全部開放 | 必填才能用 admin 功能，其餘功能不受影響 |
| `PUBLIC_BASE_URL` | 這個後端自己的對外網址，新收錄店家的照片會存成指到這個網址的 `/api/places/photo` 連結 | 選填，預設 `http://127.0.0.1:8000`，**正式環境一定要設成 Cloud Run 網址**，不然收錄進來的店家照片會連去 localhost，正式環境看不到圖 |

## 部署後端到 Cloud Run

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_backend
gcloud run deploy sugartopia-backend --source . --region asia-east1
```

第一次在新機器上跑，`gcloud` 還沒登入的話先做一次：

```bash
gcloud auth login
gcloud config set project project-06b353aa-b188-498b-ab0
```

部署大約需要幾分鐘（build container 最久），完成後會印出 `Service URL` 確認成功——正常情況下網址不會變（還是上面那個 `sugartopia-backend-673387630043.asia-east1.run.app`），只是換一個新的 revision 在跑。

**什麼時候需要重新部署**：

- 改了 `main.py` 或任何後端程式碼。
- **在 admin 頁面新增了店家、想讓 AI 問答也認識它**：AI 問答的向量資料庫只在後端啟動的當下讀取一次店家資料，之後在 admin 加的新店家會出現在 `/api/shops`、分類頁、搜尋（這些是即時查資料庫，不受影響），但 AI 問答要等下一次重新部署（讓後端重新開機一次）才會知道這家店存在。目前沒有「新增店家後立刻讓 AI 認識」的機制，只能靠重新部署。

### 更新 Cloud Run 環境變數

不用重新部署整個服務，`--update-env-vars` 可以只更新環境變數（保留其他既有的環境變數，不會覆蓋掉）：

```bash
gcloud run services update sugartopia-backend \
  --region asia-east1 \
  --update-env-vars KEY1=value1,KEY2=value2
```

也可以在 Cloud Run 主控台 → 服務 → 「編輯並部署新修訂版本」→「變數與密鑰」分頁裡改。

### 常見部署錯誤

**IAM 權限不足**（`PERMISSION_DENIED: Build failed because the default service account is missing required IAM permissions`）：

```bash
gcloud projects add-iam-policy-binding project-06b353aa-b188-498b-ab0 \
  --member=serviceAccount:673387630043-compute@developer.gserviceaccount.com \
  --role=roles/run.builder
```

跑完等 1～2 分鐘再重新部署一次。

## 部署前端到 Vercel

不用手動下指令：`SugarTopia_nuxt` repo 跟 Vercel 專案是 git-linked，`git push` 到 `main` 分支就會自動觸發部署，Vercel 上看得到部署進度跟結果。

## GitHub push 流程

兩個 repo（`SugarTopia_backend`、`SugarTopia_nuxt`）都是一般的 `git add` / `git commit` / `git push` 流程，沒有特殊步驟。`.env`、`venv/`、`__pycache__/`（後端）跟 `node_modules/`（前端）都已經在 `.gitignore` 裡，不會被追蹤到。

## 資料庫

Supabase 代管的 PostgreSQL，本機開發跟正式環境（Cloud Run）**共用同一個資料庫**，不是各自獨立的——這是刻意的取捨，不是還沒做資料庫隔離：好處是本機測試看到的資料就是真的資料，不用另外維護一份假資料；代價是本機測試（包含 Playwright 自動化測試）寫的帳號/收藏/評論都會真的留在正式資料庫裡。

## 跑 Playwright 測試（前端）

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_nuxt
npx playwright test
```

跑之前後端要先在本機啟動（`http://127.0.0.1:8000`，見上面「本機開發」），測試裡 `auth.spec.ts`／`favorites.spec.ts`／`reviews.spec.ts` 會真的建立帳號、寫評論、加收藏進資料庫（跟上面「資料庫」提到的共用資料庫是同一件事）；`reviews.spec.ts` 跑完會自動刪掉它自己建立的評論，但建立的測試帳號目前沒有辦法刪除（後端沒有刪除帳號的 API），會留在資料庫裡。

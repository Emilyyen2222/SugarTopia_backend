# SugarTopia Backend

SugarTopia 的 FastAPI 後端服務，負責提供店家資料、會員系統、收藏／評論、Google Places 店家收錄，以及 AI 甜點推薦 API。

- **想知道怎麼跑起來、怎麼部署、環境變數要填什麼** → 看 [`DEPLOYMENT.md`](./DEPLOYMENT.md)。
- **想知道某個設計為什麼這樣做、踩過什麼雷** → 看 [`BACKEND_LEARNING_NOTES.md`](./BACKEND_LEARNING_NOTES.md)。
- **想知道接下來還可以做什麼** → 看 [`PROJECT_ROADMAP.md`](./PROJECT_ROADMAP.md)。

## Online URLs

Frontend: `https://sugartopia.vercel.app`

Backend: `https://sugartopia-backend-673387630043.asia-east1.run.app`

API docs（Swagger UI）: `https://sugartopia-backend-673387630043.asia-east1.run.app/docs`

## Main APIs

Shop list（支援 `q`／`location`／`category` 篩選）：

```text
GET /api/shops?q=matcha&location=songshan
```

Shop detail：

```text
GET /api/shops/{id}
```

AI chat（依 SugarTopia 收錄的店家資料回答推薦問題）：

```text
POST /api/chat
{ "message": "我想吃抹茶甜點，有推薦嗎？" }
```

會員系統：`POST /api/auth/signup`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`

收藏：`GET/POST /api/favorites`、`DELETE /api/favorites/{shop_id}`

評論：`GET/POST /api/shops/{id}/reviews`、`PUT/DELETE /api/reviews/{id}`、`GET /api/reviews/latest`

Google Places 店家收錄（需要登入 `ADMIN_EMAILS` 白名單帳號，見 `DEPLOYMENT.md`）：`GET /api/google/places/search`、`POST /api/shops/curated`

完整清單、每支 API 實際的 request/response 格式，直接看上面的 API 文件頁最準確。

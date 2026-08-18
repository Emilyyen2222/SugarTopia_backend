# SugarTopia Backend

SugarTopia 的 FastAPI 後端服務，負責提供 AI 甜點推薦 API。

## Online URLs

Frontend:

```text
https://sugar-topia.vercel.app
```

Backend:

```text
https://sugartopia-backend-673387630043.asia-east1.run.app
```

API docs:

```text
https://sugartopia-backend-673387630043.asia-east1.run.app/docs
```

## Main APIs

Shop list:

```text
GET /api/shops
```

Example:

```text
GET /api/shops?q=matcha&location=songshan
```

Response:

```json
{
  "total": 1,
  "shops": [
    {
      "id": "matcha-mori-house",
      "name": "Matcha Mori House",
      "category": "Japanese Dessert",
      "location": "Songshan, Taipei"
    }
  ]
}
```

AI chat:

```text
POST /api/chat
```

Request:

```json
{
  "message": "我想吃抹茶甜點，有推薦嗎？"
}
```

Response:

```json
{
  "reply": "推薦內容..."
}
```

## Local Development

```bash
cd /Users/mike/Documents/emily_project_archive/SugarTopia_backend
source venv/bin/activate
uvicorn main:app --reload
```

Local backend:

```text
http://127.0.0.1:8000
```

## Environment Variables

Local development uses `.env`.

Cloud Run uses service environment variables.

Required:

```env
GOOGLE_API_KEY=your Gemini API key
GEMINI_MODEL=gemini-3.6-flash
GEMINI_EMBEDDING_MODEL=models/gemini-embedding-001
```

Do not commit `.env`.

## Learning Notes

Backend setup notes, deployment commands, error records, and beginner-friendly explanations are in:

```text
BACKEND_LEARNING_NOTES.md
```

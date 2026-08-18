from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import os
from dotenv import load_dotenv
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")

vector_db = None
llm = None
startup_error = None
shops = []

def format_model_content(content):
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return "\n".join(format_model_content(item) for item in content if item)

    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]

        if isinstance(content.get("content"), str):
            return content["content"]

        return json.dumps(content, ensure_ascii=False)

    return str(content)

def read_shop_data():
    with open("dessert_data_sample.json", "r", encoding="utf-8") as f:
        return json.load(f)

def normalize_shop(item):
    return {
        "id": item.get("id", item["name"]),
        "name": item["name_en"],
        "nameZh": item["name"],
        "category": item["category_en"],
        "categoryZh": item["category"],
        "location": item["location_en"],
        "locationZh": item["location"],
        "rating": item.get("rating", 0),
        "reviews": item.get("review_count", f"{len(item.get('reviews', []))} reviews"),
        "tags": item.get("tags_en", item.get("tags", [])),
        "tagsZh": item.get("tags", []),
        "image": item.get("image", ""),
        "description": item.get("description_en", item.get("description", "")),
        "descriptionZh": item.get("description", ""),
        "comments": item.get("reviews", []),
    }

def get_search_text(shop):
    values = [
        shop["name"],
        shop["nameZh"],
        shop["category"],
        shop["categoryZh"],
        shop["location"],
        shop["locationZh"],
        shop["description"],
        shop["descriptionZh"],
        " ".join(shop["tags"]),
        " ".join(shop["tagsZh"]),
        " ".join(shop["comments"]),
    ]
    return " ".join(values).lower()

try:
    dessert_data = read_shop_data()
    shops = [normalize_shop(item) for item in dessert_data]
except Exception as e:
    dessert_data = []
    startup_error = f"資料讀取失敗：{e}"
    print(f"❌ {startup_error}")

try:
    if not GOOGLE_API_KEY:
        raise RuntimeError(
            "Missing Google Gemini API key. Set GOOGLE_API_KEY or GEMINI_API_KEY in your environment."
        )

    # 1. 建立 Embedding 模型
    embeddings = GoogleGenerativeAIEmbeddings(model=GEMINI_EMBEDDING_MODEL, google_api_key=GOOGLE_API_KEY)

    # 2. 讀取 dessert_data_sample.json 資料
    try:
        documents = []
        for item in dessert_data:
            content = (
                f"店名：{item['name']}\n"
                f"英文名稱：{item['name_en']}\n"
                f"分類：{item['category']}\n"
                f"地點：{item['location']}\n"
                f"評分：{item.get('rating', '暫無評分')}\n"
                f"特色標籤：{', '.join(item['tags'])}\n"
                f"介紹：{item.get('description', '')}\n"
                f"評論：{' '.join(item['reviews'])}"
            )
            documents.append(Document(page_content=content))

        # 3. 建立 Chroma 向量資料庫
        vector_db = Chroma.from_documents(documents, embeddings)
        print("✅ 甜點資料庫載入成功！")
    except Exception as e:
        raise RuntimeError(f"資料庫載入失敗，請確認檔案與 Gemini API key 是否正確。錯誤: {e}") from e

    # 4. 設定 Gemini 語言模型 (改用 LangChain 模組解決棄用警告)
    llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL, google_api_key=GOOGLE_API_KEY)
except Exception as e:
    startup_error = str(e)
    print(f"❌ AI 服務初始化失敗: {startup_error}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sugar-topia.vercel.app",
        "http://127.0.0.1:5501",
        "http://localhost:5501",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def read_root():
    return {
        "message": "SugarTopia backend is running.",
        "chat_api": "POST /api/chat",
        "docs": "/docs",
    }

@app.get("/health")
def health_check():
    return {
        "status": "ok" if vector_db is not None and llm is not None else "error",
        "detail": startup_error,
    }

@app.get("/api/shops")
def get_shops(q: str = "", location: str = "", category: str = ""):
    query = q.lower().strip()
    place = location.lower().strip()
    shop_category = category.lower().strip()

    results = []
    for shop in shops:
        search_text = get_search_text(shop)
        matches_query = not query or query in search_text
        matches_location = not place or place in search_text
        matches_category = not shop_category or shop_category in search_text

        if matches_query and matches_location and matches_category:
            results.append(shop)

    return {
        "total": len(results),
        "shops": results,
    }

@app.post("/api/chat")
def chat_with_gemini(request: ChatRequest):
    if vector_db is None or llm is None:
        raise HTTPException(
            status_code=503,
            detail=startup_error or "AI service is not initialized.",
        )

    try:
        # A. 搜尋最相關的甜點資料
        search_results = vector_db.similarity_search(request.message, k=min(4, len(dessert_data)))
        
        # B. 整理 Context
        context = ""
        for i, doc in enumerate(search_results):
            context += f"\n--- 參考資料 {i+1} ---\n{doc.page_content}\n"
        
        # C. 組合專屬 Prompt
        prompt = f"""
        你是一個專業的台北甜點推薦助手。請嚴格根據以下【參考資料】來回答使用者的問題。
        如果參考資料中有符合使用者需求的店家，請直接推薦 1 到 2 間，並說明推薦原因。
        如果只有部分符合，也可以清楚說明「目前資料中最接近的是...」。
        如果參考資料中完全沒有相關資訊，請直接回答「抱歉，目前的資料庫中沒有找到相關的甜點資訊。」，絕對不可以自己隨機編造資料庫以外的店家。
        請用自然段落或簡短條列回答，不要使用 Markdown 格式符號，例如 **、###、```。

        【參考資料】:
        {context}

        【使用者的問題】:
        {request.message}
        """
        
        # D. 呼叫 Gemini 產生回答
        response = llm.invoke(prompt)
        return {"reply": format_model_content(response.content)}
    except Exception as e:
        return {"error": str(e)}

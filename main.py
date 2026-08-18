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

try:
    if not GOOGLE_API_KEY:
        raise RuntimeError(
            "Missing Google Gemini API key. Set GOOGLE_API_KEY or GEMINI_API_KEY in your environment."
        )

    # 1. 建立 Embedding 模型
    embeddings = GoogleGenerativeAIEmbeddings(model=GEMINI_EMBEDDING_MODEL, google_api_key=GOOGLE_API_KEY)

    # 2. 讀取 dessert_data_sample.json 資料
    try:
        with open('dessert_data_sample.json', 'r', encoding='utf-8') as f:
            dessert_data = json.load(f)

        documents = []
        for item in dessert_data:
            content = f"店名：{item['name']}\n分類：{item['category']}\n地點：{item['location']}\n特色標籤：{', '.join(item['tags'])}\n評論：{' '.join(item['reviews'])}"
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
    allow_origins=["https://sugar-topia.vercel.app"],
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

@app.post("/api/chat")
def chat_with_gemini(request: ChatRequest):
    if vector_db is None or llm is None:
        raise HTTPException(
            status_code=503,
            detail=startup_error or "AI service is not initialized.",
        )

    try:
        # A. 搜尋最相關的 2 筆甜點資料
        search_results = vector_db.similarity_search(request.message, k=2)
        
        # B. 整理 Context
        context = ""
        for i, doc in enumerate(search_results):
            context += f"\n--- 參考資料 {i+1} ---\n{doc.page_content}\n"
        
        # C. 組合專屬 Prompt
        prompt = f"""
        你是一個專業的台北甜點推薦助手。請嚴格根據以下【參考資料】來回答使用者的問題。
        如果參考資料中沒有相關資訊，請直接回答「抱歉，目前的資料庫中沒有找到相關的甜點資訊。」，絕對不可以自己隨機編造資料庫以外的店家。
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

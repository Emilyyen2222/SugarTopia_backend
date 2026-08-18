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

def classify_question(message):
    text = message.lower().strip()

    out_of_scope_keywords = [
        "股票", "股市", "投資", "基金", "匯率", "天氣", "政治", "選舉",
        "python", "javascript", "vue", "程式", "履歷", "面試",
        "stock", "weather", "politics", "election", "crypto", "bitcoin",
    ]
    dessert_keywords = [
        "甜點", "蛋糕", "布丁", "抹茶", "咖啡", "咖啡廳", "下午茶", "冰淇淋",
        "可麗露", "肉桂捲", "貝果", "舒芙蕾", "乳酪", "起司", "巧克力",
        "推薦", "店", "台北", "大安", "松山", "信義", "中山", "內湖",
        "約會", "讀書", "工作", "安靜", "外帶", "dessert", "cake", "pudding",
        "matcha", "coffee", "cafe", "gelato", "bakery", "taipei",
    ]
    knowledge_keywords = [
        "是什麼", "怎麼選", "差別", "介紹", "做法", "口感", "熱量", "保存",
        "what is", "how to choose", "difference", "explain",
    ]

    has_dessert_signal = any(keyword in text for keyword in dessert_keywords)
    has_knowledge_signal = any(keyword in text for keyword in knowledge_keywords)

    if any(keyword in text for keyword in out_of_scope_keywords) and not has_dessert_signal:
        return "out_of_scope"

    if has_knowledge_signal:
        return "dessert_knowledge"

    if has_dessert_signal:
        return "shop_recommendation"

    return "out_of_scope"

def format_documents(docs):
    if not docs:
        return "目前沒有找到完全符合的 SugarTopia 店家資料。"

    context = ""
    for i, doc in enumerate(docs):
        context += f"\n--- 參考資料 {i+1} ---\n{doc.page_content}\n"

    return context

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
        question_type = classify_question(request.message)

        if question_type == "out_of_scope":
            return {
                "reply": "我是 SugarTopia 甜點推薦助手，主要可以幫你找甜點店、咖啡廳、甜點種類介紹，或依照地區和情境推薦店家。你可以問我：想吃抹茶甜點、想找適合工作的咖啡廳，或布丁和奶酪有什麼差別。"
            }

        search_results = []
        if question_type == "shop_recommendation":
            search_results = vector_db.similarity_search(request.message, k=min(4, len(dessert_data)))

        context = format_documents(search_results)

        if question_type == "dessert_knowledge":
            task_instruction = """
            使用者正在詢問甜點、咖啡廳或用餐情境的知識型問題。
            你可以使用一般甜點知識回答，不需要硬推薦店家。
            如果 SugarTopia 參考資料中剛好有相關店家，可以在最後補一句「如果想找店家，SugarTopia 目前資料中可參考...」。
            """
        else:
            task_instruction = """
            使用者正在尋找甜點店推薦。
            請優先根據 SugarTopia 參考資料推薦 1 到 2 間店，並說明推薦原因。
            如果只有部分符合，也可以清楚說明「目前 SugarTopia 資料中最接近的是...」。
            如果參考資料中完全沒有相關店家，請不要編造不存在的店家，可以改給甜點選擇建議，並說明目前 SugarTopia 資料還沒有收錄完全符合的店家。
            """

        prompt = f"""
        你是 SugarTopia 的甜點推薦助手，熟悉台北甜點店、甜點種類、咖啡廳情境與用餐需求。

        【問題類型】:
        {question_type}

        【回答規則】:
        {task_instruction}

        不可以編造 SugarTopia 參考資料以外的店家名稱。
        可以回答甜點相關常識，但若提到店家推薦，必須來自 SugarTopia 參考資料。
        回答要自然、親切、具體，避免太長。
        請用自然段落或簡短條列回答，不要使用 Markdown 格式符號，例如 **、###、```。

        【SugarTopia 參考資料】:
        {context}

        【使用者的問題】:
        {request.message}
        """
        
        response = llm.invoke(prompt)
        return {
            "reply": format_model_content(response.content),
            "type": question_type,
        }
    except Exception as e:
        return {"error": str(e)}

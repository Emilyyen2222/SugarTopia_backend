from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import bcrypt
from datetime import datetime, timedelta, timezone
import json
import os
import re
import requests
import secrets
import sqlite3
from dotenv import load_dotenv
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")
# 這把 key 是另外申請的 Google Maps Platform key，故意跟上面 Gemini 用的
# GOOGLE_API_KEY 分開，避免混用（兩個是不同的 Google Cloud 服務、不同的計費）。
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
GOOGLE_PLACES_BASE_URL = "https://places.googleapis.com/v1"
DATABASE_PATH = os.getenv("DATABASE_PATH", "sugartopia_app.db")
SESSION_HOURS = 24 * 7

vector_db = None
llm = None
startup_error = None
shops = []

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_app_database():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                shop_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(user_id, shop_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                shop_id TEXT NOT NULL,
                rating INTEGER NOT NULL,
                review_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS curated_shops (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_zh TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                category_zh TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                location_zh TEXT NOT NULL DEFAULT '',
                rating REAL,
                review_count INTEGER,
                tags TEXT NOT NULL DEFAULT '[]',
                tags_zh TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                lat REAL,
                lng REAL,
                google_place_id TEXT NOT NULL UNIQUE,
                google_maps_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

        # curated_shops 這張表在加上 lat/lng 之前就已經存在，SQLite 的
        # CREATE TABLE IF NOT EXISTS 不會回頭幫舊表補新欄位，所以這裡手動檢查、
        # 缺就用 ALTER TABLE 補上，讓已經收錄過的店家舊資料不會壞掉。
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(curated_shops)").fetchall()
        }
        if "lat" not in existing_columns:
            conn.execute("ALTER TABLE curated_shops ADD COLUMN lat REAL")
        if "lng" not in existing_columns:
            conn.execute("ALTER TABLE curated_shops ADD COLUMN lng REAL")

        conn.commit()

def serialize_user(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "createdAt": row["created_at"],
    }

def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(password, password_hash):
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))

def create_session(user_id):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_HOURS)

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, expires_at.isoformat(), now.isoformat()),
        )
        conn.commit()

    return {
        "token": token,
        "expiresAt": expires_at.isoformat(),
    }

def get_bearer_token(authorization):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")

    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Invalid Authorization header.")

    return authorization[len(prefix):].strip()

def get_current_user_from_token(token):
    now = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.name, users.email, users.created_at, sessions.expires_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=401, detail="Session not found.")

        if row["expires_at"] <= now:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            raise HTTPException(status_code=401, detail="Session expired.")

        return row

def require_current_user(authorization):
    token = get_bearer_token(authorization)
    return get_current_user_from_token(token)

def normalize_email(email):
    normalized = email.strip().lower()

    if "@" not in normalized or "." not in normalized.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Please enter a valid email.")

    return normalized

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

# dessert_data_sample.json 裡的店家是示意資料，沒有真實地址，所以沒有真實經緯度可用。
# 這裡用「行政區中心點」當作大概位置，讓地圖至少落在正確的區，不是精確門牌，
# 跟 Google Places 收錄進來、有真實經緯度的店家（curated_shops）要分開看待。
TAIPEI_DISTRICT_COORDINATES = {
    "songshan, taipei": (25.0500, 121.5578),
    "da'an, taipei": (25.0263, 121.5432),
    "zhongshan, taipei": (25.0623, 121.5262),
    "xinyi, taipei": (25.0330, 121.5654),
    "neihu, taipei": (25.0693, 121.5885),
}

def get_district_coordinates(location_en):
    return TAIPEI_DISTRICT_COORDINATES.get((location_en or "").strip().lower())

def normalize_shop(item):
    coordinates = get_district_coordinates(item.get("location_en"))

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
        "lat": coordinates[0] if coordinates else None,
        "lng": coordinates[1] if coordinates else None,
    }

def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "shop"

def normalize_curated_shop(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "nameZh": row["name_zh"] or row["name"],
        "category": row["category"],
        "categoryZh": row["category_zh"],
        "location": row["location"],
        "locationZh": row["location_zh"],
        "rating": row["rating"] or 0,
        "reviews": f"{row['review_count']} reviews" if row["review_count"] else "No reviews yet",
        "tags": json.loads(row["tags"] or "[]"),
        "tagsZh": json.loads(row["tags_zh"] or "[]"),
        "image": row["image"] or "",
        "description": row["description"] or "",
        "descriptionZh": row["description"] or "",
        "comments": [],
        "source": "google_places",
        "googleMapsUrl": row["google_maps_url"],
        "lat": row["lat"],
        "lng": row["lng"],
    }

def load_curated_shops():
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM curated_shops ORDER BY created_at ASC").fetchall()

    return [normalize_curated_shop(row) for row in rows]

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

def find_shop(shop_id):
    for shop in shops:
        if shop["id"] == shop_id:
            return shop

    return None

def serialize_review(row):
    return {
        "id": row["id"],
        "shopId": row["shop_id"],
        "rating": row["rating"],
        "text": row["review_text"],
        "createdAt": row["created_at"],
        "reviewerName": row["name"],
    }

def serialize_google_place(place):
    location = place.get("location", {})
    return {
        "placeId": place.get("id"),
        "name": place.get("displayName", {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "rating": place.get("rating"),
        "reviewCount": place.get("userRatingCount"),
        "googleMapsUrl": place.get("googleMapsUri"),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
    }

def serialize_google_place_details(place):
    details = serialize_google_place(place)
    hours = place.get("currentOpeningHours", {})
    details.update({
        "phone": place.get("internationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "openNow": hours.get("openNow"),
        "weekdayDescriptions": hours.get("weekdayDescriptions", []),
    })
    return details

def get_google_places_error_detail(response):
    try:
        return response.json().get("error", {}).get("message", "Google Places API request failed.")
    except ValueError:
        return "Google Places API request failed."

def classify_question(message):
    text = message.lower().strip()

    out_of_scope_keywords = [
        "股票", "股市", "投資", "基金", "匯率", "天氣", "政治", "選舉",
        "python", "javascript", "vue", "程式", "履歷", "面試",
        "stock", "weather", "politics", "election", "crypto", "bitcoin",
    ]
    unrealistic_request_keywords = [
        "請讓我", "讓我跟", "見面", "約出來", "幫我約", "變成", "扮演",
        "生成圖片", "產生圖片", "畫一張", "幫我畫", "make me meet",
        "meet with", "roleplay", "draw", "generate image",
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
    has_unrealistic_request = any(keyword in text for keyword in unrealistic_request_keywords)

    if has_unrealistic_request and has_dessert_signal:
        return "shop_recommendation"

    if has_unrealistic_request:
        return "out_of_scope"

    if any(keyword in text for keyword in out_of_scope_keywords) and not has_dessert_signal:
        return "out_of_scope"

    if has_knowledge_signal:
        return "dessert_knowledge"

    if has_dessert_signal:
        return "shop_recommendation"

    return "out_of_scope"

def includes_unrealistic_request(message):
    text = message.lower().strip()
    unrealistic_request_keywords = [
        "請讓我", "讓我跟", "見面", "約出來", "幫我約", "變成", "扮演",
        "生成圖片", "產生圖片", "畫一張", "幫我畫", "make me meet",
        "meet with", "roleplay", "draw", "generate image",
    ]
    return any(keyword in text for keyword in unrealistic_request_keywords)

def format_documents(docs):
    if not docs:
        return "目前沒有找到完全符合的 SugarTopia 店家資料。"

    context = ""
    for i, doc in enumerate(docs):
        context += f"\n--- 參考資料 {i+1} ---\n{doc.page_content}\n"

    return context

def get_public_ai_error(error):
    error_text = str(error)
    print(f"❌ Gemini request failed: {error_text}")

    if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text or "quota" in error_text.lower():
        return "SugarTopia AI 目前使用量已達上限，請晚一點再試。"

    if "API key" in error_text or "INVALID_ARGUMENT" in error_text:
        return "SugarTopia AI 目前設定需要檢查，請稍後再試。"

    return "SugarTopia AI 目前有點忙，請稍後再試。"

try:
    dessert_data = read_shop_data()
    shops = [normalize_shop(item) for item in dessert_data]
except Exception as e:
    dessert_data = []
    startup_error = f"資料讀取失敗：{e}"
    print(f"❌ {startup_error}")

try:
    init_app_database()
    print("✅ SugarTopia 會員資料庫準備完成！")

    curated_shops = load_curated_shops()
    shops.extend(curated_shops)
    if curated_shops:
        print(f"✅ 讀取到 {len(curated_shops)} 家從 Google Places 收錄的店家！")
except Exception as e:
    startup_error = f"會員資料庫初始化失敗：{e}"
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
        # SugarTopia_nuxt（Nuxt 重構版）正式上線的網址，跟舊版 vanilla 的
        # sugar-topia.vercel.app（有連字號）是不同網域，CORS 是照網域整個
        # 比對、不會因為「看起來很像」就放行，這條漏加會讓正式環境上的
        # AI 問答（以及所有其他 API 呼叫）被瀏覽器直接擋掉，前端看到的
        # 症狀是「連不上後端」，但其實後端本身完全正常，只是 CORS 擋住。
        "https://sugartopia.vercel.app",
        "http://127.0.0.1:5501",
        "http://localhost:5501",
        "http://[::]:5501",
        "http://[::1]:5501",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://[::]:5500",
        "http://[::1]:5500",
        # SugarTopia_nuxt（Nuxt 重構版）本機開發用的 port，Nuxt 預設跑在
        # 3000。這個新前端還在遷移中，跟 vanilla 版本共用同一個後端。
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://[::]:3000",
        "http://[::1]:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str

class SignupRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class FavoriteRequest(BaseModel):
    shop_id: str

class ReviewRequest(BaseModel):
    rating: int
    text: str

class CuratedShopRequest(BaseModel):
    place_id: str
    category: str = ""
    category_zh: str = ""
    tags: list[str] = []
    tags_zh: list[str] = []

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
        "database": os.path.basename(DATABASE_PATH),
    }

@app.post("/api/auth/signup")
def signup(request: SignupRequest):
    name = request.name.strip()
    email = normalize_email(request.email)
    password = request.password

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters.")

    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    now = datetime.now(timezone.utc).isoformat()

    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (name, email, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, email, hash_password(password), now),
            )
            conn.commit()
            user_id = cursor.lastrowid
            user = conn.execute(
                "SELECT id, name, email, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="This email is already registered.")

    session = create_session(user_id)

    return {
        "user": serialize_user(user),
        "token": session["token"],
        "expiresAt": session["expiresAt"],
    }

@app.post("/api/auth/login")
def login(request: LoginRequest):
    email = normalize_email(request.email)

    with get_db_connection() as conn:
        user = conn.execute(
            "SELECT id, name, email, password_hash, created_at FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    if user is None or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")

    session = create_session(user["id"])

    return {
        "user": serialize_user(user),
        "token": session["token"],
        "expiresAt": session["expiresAt"],
    }

@app.get("/api/auth/me")
def get_me(authorization: str = Header(default="")):
    user = require_current_user(authorization)

    return {
        "user": serialize_user(user),
    }

@app.post("/api/auth/logout")
def logout(authorization: str = Header(default="")):
    token = get_bearer_token(authorization)

    with get_db_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()

    return {
        "message": "Logged out successfully.",
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

@app.get("/api/shops/{shop_id}")
def get_shop(shop_id: str):
    shop = find_shop(shop_id)

    if shop is None:
        raise HTTPException(status_code=404, detail="Shop not found.")

    return shop

@app.get("/api/favorites")
def get_favorites(authorization: str = Header(default="")):
    user = require_current_user(authorization)

    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT shop_id FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()

    favorite_ids = {row["shop_id"] for row in rows}
    favorite_shops = [shop for shop in shops if shop["id"] in favorite_ids]

    return {
        "total": len(favorite_shops),
        "shops": favorite_shops,
    }

@app.post("/api/favorites")
def add_favorite(request: FavoriteRequest, authorization: str = Header(default="")):
    user = require_current_user(authorization)

    if find_shop(request.shop_id) is None:
        raise HTTPException(status_code=404, detail="Shop not found.")

    now = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO favorites (user_id, shop_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, shop_id) DO NOTHING
            """,
            (user["id"], request.shop_id, now),
        )
        conn.commit()

    return {"message": "Shop saved to favorites.", "shopId": request.shop_id}

@app.delete("/api/favorites/{shop_id}")
def remove_favorite(shop_id: str, authorization: str = Header(default="")):
    user = require_current_user(authorization)

    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM favorites WHERE user_id = ? AND shop_id = ?",
            (user["id"], shop_id),
        )
        conn.commit()

    return {"message": "Shop removed from favorites.", "shopId": shop_id}

@app.get("/api/shops/{shop_id}/reviews")
def get_shop_reviews(shop_id: str):
    if find_shop(shop_id) is None:
        raise HTTPException(status_code=404, detail="Shop not found.")

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT reviews.id, reviews.shop_id, reviews.rating, reviews.review_text, reviews.created_at, users.name
            FROM reviews
            JOIN users ON users.id = reviews.user_id
            WHERE reviews.shop_id = ?
            ORDER BY reviews.created_at DESC
            """,
            (shop_id,),
        ).fetchall()

    review_list = [serialize_review(row) for row in rows]

    return {
        "total": len(review_list),
        "reviews": review_list,
    }

@app.post("/api/shops/{shop_id}/reviews")
def add_shop_review(shop_id: str, request: ReviewRequest, authorization: str = Header(default="")):
    user = require_current_user(authorization)

    if find_shop(shop_id) is None:
        raise HTTPException(status_code=404, detail="Shop not found.")

    if request.rating < 1 or request.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")

    review_text = request.text.strip()
    if len(review_text) < 2:
        raise HTTPException(status_code=400, detail="Review text is too short.")

    now = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reviews (user_id, shop_id, rating, review_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user["id"], shop_id, request.rating, review_text, now),
        )
        conn.commit()
        review_id = cursor.lastrowid
        row = conn.execute(
            """
            SELECT reviews.id, reviews.shop_id, reviews.rating, reviews.review_text, reviews.created_at, users.name
            FROM reviews
            JOIN users ON users.id = reviews.user_id
            WHERE reviews.id = ?
            """,
            (review_id,),
        ).fetchone()

    return serialize_review(row)

@app.get("/api/reviews/latest")
def get_latest_reviews(limit: int = 8):
    capped_limit = max(1, min(limit, 20))

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT reviews.id, reviews.shop_id, reviews.rating, reviews.review_text, reviews.created_at, users.name
            FROM reviews
            JOIN users ON users.id = reviews.user_id
            ORDER BY reviews.created_at DESC
            LIMIT ?
            """,
            (capped_limit,),
        ).fetchall()

    review_list = []
    for row in rows:
        review = serialize_review(row)
        shop = find_shop(row["shop_id"])
        review["shopName"] = shop["name"] if shop else row["shop_id"]
        review["shopImage"] = shop["image"] if shop else ""
        review_list.append(review)

    return {
        "total": len(review_list),
        "reviews": review_list,
    }

@app.get("/api/google/places/search")
def search_google_places(q: str = ""):
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=503, detail="Google Places API key is not configured.")

    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query parameter q is required.")

    try:
        response = requests.post(
            f"{GOOGLE_PLACES_BASE_URL}/places:searchText",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": (
                    "places.id,places.displayName,places.formattedAddress,"
                    "places.rating,places.userRatingCount,places.googleMapsUri,places.location"
                ),
            },
            json={"textQuery": query},
            timeout=10,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach Google Places API.")

    if not response.ok:
        raise HTTPException(status_code=502, detail=get_google_places_error_detail(response))

    places = [serialize_google_place(place) for place in response.json().get("places", [])]

    return {
        "total": len(places),
        "places": places,
    }

@app.get("/api/google/places/{place_id}")
def get_google_place(place_id: str):
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=503, detail="Google Places API key is not configured.")

    try:
        response = requests.get(
            f"{GOOGLE_PLACES_BASE_URL}/places/{place_id}",
            headers={
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": (
                    "id,displayName,formattedAddress,rating,userRatingCount,googleMapsUri,"
                    "location,internationalPhoneNumber,websiteUri,"
                    "currentOpeningHours.openNow,currentOpeningHours.weekdayDescriptions"
                ),
            },
            timeout=10,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach Google Places API.")

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Place not found.")

    if not response.ok:
        raise HTTPException(status_code=502, detail=get_google_places_error_detail(response))

    return serialize_google_place_details(response.json())

@app.post("/api/shops/curated")
def add_curated_shop(request: CuratedShopRequest):
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=503, detail="Google Places API key is not configured.")

    place_id = request.place_id.strip()
    if not place_id:
        raise HTTPException(status_code=400, detail="place_id is required.")

    # 如果這個 place_id 已經收錄過，直接回傳現有資料，不要重複新增
    # （點兩次「加入」按鈕結果要一樣，這跟收藏功能的 UPSERT 是同一個概念）。
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM curated_shops WHERE google_place_id = ?", (place_id,)
        ).fetchone()

    if existing:
        return {"message": "This place is already in SugarTopia.", "shop": find_shop(existing["id"])}

    # 不直接信任前端傳來的店名／地址／評分，而是拿 place_id 重新問一次 Google，
    # 確保存進資料庫的資料，跟後端當下向 Google 查證的資料是同一份。
    try:
        response = requests.get(
            f"{GOOGLE_PLACES_BASE_URL}/places/{place_id}",
            headers={
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": (
                    "id,displayName,formattedAddress,rating,userRatingCount,"
                    "googleMapsUri,location"
                ),
            },
            timeout=10,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach Google Places API.")

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Place not found.")

    if not response.ok:
        raise HTTPException(status_code=502, detail=get_google_places_error_detail(response))

    place = response.json()
    name = place.get("displayName", {}).get("text", "").strip()
    if not name:
        raise HTTPException(status_code=502, detail="Google did not return a shop name.")

    location = place.get("location", {})
    shop_id = f"{slugify(name)}-{place_id[-6:].lower()}"
    now = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO curated_shops (
                id, name, name_zh, category, category_zh, location, location_zh,
                rating, review_count, tags, tags_zh, description, image, lat, lng,
                google_place_id, google_maps_url, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(google_place_id) DO NOTHING
            """,
            (
                shop_id,
                name,
                name,
                request.category,
                request.category_zh,
                place.get("formattedAddress", ""),
                place.get("formattedAddress", ""),
                place.get("rating"),
                place.get("userRatingCount"),
                json.dumps(request.tags, ensure_ascii=False),
                json.dumps(request.tags_zh, ensure_ascii=False),
                "",
                "",
                location.get("latitude"),
                location.get("longitude"),
                place_id,
                place.get("googleMapsUri", ""),
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM curated_shops WHERE google_place_id = ?", (place_id,)
        ).fetchone()

    new_shop = normalize_curated_shop(row)
    shops.append(new_shop)

    return {"message": "Shop added to SugarTopia.", "shop": new_shop}

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
                "reply": "我是 SugarTopia 甜點推薦助手，主要可以幫你找甜點店、咖啡廳、甜點種類介紹，或依照地區和情境推薦店家。你可以問我：想吃抹茶甜點、想找適合工作的咖啡廳，或布丁和奶酪有什麼差別。",
                "type": question_type,
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

        boundary_instruction = ""
        if includes_unrealistic_request(request.message):
            boundary_instruction = """
            使用者的問題中包含 SugarTopia 做不到的要求，例如安排和特定人物見面、約出來、角色扮演或生成圖片。
            請用一句話溫和說明你不能完成那個部分，但不要整題拒絕。
            接著只針對問題裡和甜點、下午茶、咖啡廳、地點、心情或用餐情境有關的部分回答。
            """

        prompt = f"""
        你是 SugarTopia 的甜點推薦助手，熟悉台北甜點店、甜點種類、咖啡廳情境與用餐需求。

        【問題類型】:
        {question_type}

        【回答規則】:
        {task_instruction}
        {boundary_instruction}

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
        return {
            "reply": get_public_ai_error(e),
            "type": "error",
        }

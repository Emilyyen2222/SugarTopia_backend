from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Header
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import bcrypt
from datetime import datetime, timedelta, timezone
import json
import os
import re
import requests
import secrets
from urllib.parse import quote
import psycopg2
import psycopg2.extras
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
# 首頁 hero 圖片輪播用的免費圖庫 key，跟上面兩把 key（Gemini／Google Places）
# 完全獨立，是另一家服務（Pexels）、另一組帳號申請的。
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
GOOGLE_PLACES_BASE_URL = "https://places.googleapis.com/v1"
# 存進 curated_shops 的 image 欄位需要是一個完整網址（前端 resolveShopImage()
# 看到 http/https 開頭就直接原樣使用，不會再幫忙補 host）——因為圖片是靠
# /api/places/photo 這支後端自己的路由代理出去的（見下面），跟前端不同
# origin（Vercel／Cloud Run），存相對路徑會變成去前端自己的網域找圖，404。
# 本機開發預設用本機網址，正式環境要記得在 Cloud Run 設這個環境變數。
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
# 收錄店家用的 admin 頁面／API（搜尋 Google Places、把結果寫進 curated_shops）
# 只讓這裡列出的 email 用——比對前先轉小寫、去頭尾空白，設定環境變數時
# 不用糾結大小寫或多打的空格。逗號分隔可以放多個 email，例如以後要多開一個
# 帳號給別人協助收錄。沒有設定這個環境變數的話（本機沒設、忘記在 Cloud Run
# 設），這幾支 API 會直接全部擋掉，不會「沒設定就等於誰都能用」這種故障
# 開放（fail-open）的預設值。
ADMIN_EMAILS = {email.strip().lower() for email in os.getenv("ADMIN_EMAILS", "").split(",") if email.strip()}
# 原本用 SQLite，資料庫是容器裡的一個檔案——但 Cloud Run 沒有持久化磁碟，
# 容器只要重啟（重新部署、或閒置太久被自動回收）裡面的檔案就會全部消失，
# 這也是之前店家/評論資料一直不見的原因。改用 Supabase 代管的 PostgreSQL，
# 資料庫獨立於 Cloud Run 之外，容器重啟不會再影響到資料。
DATABASE_URL = os.getenv("DATABASE_URL")
SESSION_HOURS = 24 * 7

vector_db = None
llm = None
startup_error = None
shops = []
vector_document_count = 0

# 這個小 wrapper 讓程式碼其餘部分（conn.execute(...)、row["欄位名稱"]、
# with get_db_connection() as conn: 這些寫法）幾乎不用改，把底層從
# sqlite3 換成 psycopg2 時只集中改這裡：
# - SQLite 用 "?" 佔位符，Postgres 用 "%s"，這裡自動轉換。
# - 用 RealDictCursor 讓每一列資料表現得像 dict，跟原本 sqlite3.Row 的
#   row["col"] 用法相容。
# - with 區塊結束時沒有例外就自動 commit，有例外就 rollback，並且真的把
#   連線關掉（sqlite3 原本的 with 只會 commit/rollback，不會關閉連線；
#   Postgres 連線數有限，用完一定要關）。
class DBConnection:
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql, params=()):
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(sql.replace("?", "%s"), params)
        return cursor

    def commit(self):
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()

def get_db_connection():
    return DBConnection(psycopg2.connect(DATABASE_URL))

def init_app_database():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                shop_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(user_id, shop_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                shop_id TEXT NOT NULL,
                rating INTEGER NOT NULL,
                review_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        # reviews 表在正式環境已經有真實資料了，CREATE TABLE IF NOT EXISTS
        # 不會幫已存在的表補新欄位——這裡另外用 ALTER TABLE ADD COLUMN IF
        # NOT EXISTS 補上 context_tags（Phase 4「情境式心得」用），
        # IF NOT EXISTS 讓這行每次開機都能安全重複執行，不會因為欄位已經
        # 存在就噴錯。
        conn.execute("""
            ALTER TABLE reviews ADD COLUMN IF NOT EXISTS context_tags TEXT NOT NULL DEFAULT '[]'
        """)
        # ai_context_tags 是 AI 從評論文字自動分析出來的標籤，跟使用者自己
        # 勾的 context_tags 分開存——前端要能分別顯示「使用者自選」跟
        # 「AI 分析」兩種標籤，不是同一批資料混在一起。
        conn.execute("""
            ALTER TABLE reviews ADD COLUMN IF NOT EXISTS ai_context_tags TEXT NOT NULL DEFAULT '[]'
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
        # 首頁 hero 輪播圖，從 Pexels 抓來的甜點/咖啡照片快取——不是每個
        # 訪客進站都即時打一次 Pexels API（免費額度不夠用，也會拖慢首頁
        # 載入），而是後端啟動時抓一批存這裡，前端只讀這張表。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hero_photos (
                id SERIAL PRIMARY KEY,
                url TEXT NOT NULL,
                photographer TEXT NOT NULL DEFAULT '',
                photographer_url TEXT NOT NULL DEFAULT '',
                pexels_url TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL
            )
        """)
        # Phase 4「甜點願望單」：使用者直接輸入一段情境需求存起來（例如
        # 「想找台北焦糖布丁、安靜、可以坐兩小時的店」），不用先篩選、
        # 不用符合固定欄位——這批資料本身代表的是「使用者真正想要什麼」，
        # 跟評論（描述已經去過的店）是不同性質的資料。MVP 版本只存文字，
        # 沒有自動比對現有店家、沒有通知機制，那些是之後才要做的延伸。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
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

def require_admin_user(authorization):
    # 先確認有登入（跟其他需要登入的 API 一樣），登入之後再多檢查一層：
    # email 有沒有在 ADMIN_EMAILS 白名單裡。不是白名單就當作一般已登入
    # 使用者，回 403（跟沒登入的 401 分開，方便前端分辨是「請登入」還是
    # 「登入了但沒有權限」）。
    user = require_current_user(authorization)

    if user["email"].strip().lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="This account does not have admin access.")

    return user

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

# 首頁 hero 輪播圖用的搜尋關鍵字，故意用好幾個不同的字分開搜，每個字
# 只抓 2 張——只搜一個關鍵字（例如都搜 "dessert"）容易抓到很像的照片
# （同一批熱門結果），分開搜幾個相關但不同的字，輪播起來畫面比較有
# 變化。
#
# "cafe"／"coffee shop"／"pastry" 這幾個字太籠統，實測會抓到不少跟
# 甜點/咖啡廳沒什麼關係的街景照（見這次調整前的觀察），換成更具體的
# 詞組（"pastry dessert"、"coffee cup"）結果明顯更切題。"dog friendly
# cafe" 是使用者要求加的，呼應網站本身「毛孩友善」這個分類，也實測過
# 確實會抓到真的有狗在咖啡廳的照片，不是隨便湊的關鍵字。
#
# 後來使用者覺得原本這組圖不好看，改成統一走日式甜點風格——每個詞組都
# 先用 curl 打過 Pexels 搜尋 API 實際看過回傳結果，確認抓到的真的是
# 和菓子/麻糬/抹茶甜點這類主題圖，不是隨便沾邊的照片（例如 "japanese
# dessert"、"mochi"、"japanese bakery" 這幾個詞組實測會混進壽司卷、
# 一般麵包架這種不相關的結果，所以沒有採用）。
PEXELS_HERO_QUERIES = ["wagashi", "mochi dessert", "matcha dessert", "dango", "matcha cafe"]

def refresh_hero_photos():
    # 跟向量資料庫、curated_shops 一樣的取捨：只在後端啟動時抓一次，
    # 不是即時抓、也沒有排程定期更新——Pexels 免費額度不高，這個是
    # 「刻意先簡單做」的版本，之後如果想做到真的每天自動換一批，可以
    # 加 Cloud Scheduler 定期打一支後端的刷新端點，而不是現在這種
    # 每次開機才換的做法。任何失敗（key 沒設定、API 打不通、額度用完）
    # 都不拋例外——首頁輪播圖抓不到新照片，不該連帶讓整個後端開機失敗。
    if not PEXELS_API_KEY:
        print("⚠️ 沒有設定 PEXELS_API_KEY，首頁 hero 輪播圖沿用前端本地圖片。")
        return

    photos = []
    for query in PEXELS_HERO_QUERIES:
        try:
            response = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": query, "per_page": 2},
                timeout=10,
            )
            response.raise_for_status()
            for photo in response.json().get("photos", []):
                photos.append({
                    "url": photo["src"]["landscape"],
                    "photographer": photo.get("photographer", ""),
                    "photographer_url": photo.get("photographer_url", ""),
                    "pexels_url": photo.get("url", ""),
                })
        except Exception as e:
            print(f"⚠️ Pexels 搜尋「{query}」失敗，跳過這個關鍵字：{e}")

    if not photos:
        print("⚠️ Pexels 一張照片都沒抓到，首頁 hero 輪播圖沿用前端本地圖片。")
        return

    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        # 每次重新抓都整批換掉，不是疊加——舊的那批快取沒有保留的必要，
        # 也不用另外寫「哪些是舊的、該刪掉」的邏輯。
        conn.execute("DELETE FROM hero_photos")
        for photo in photos:
            conn.execute(
                """
                INSERT INTO hero_photos (url, photographer, photographer_url, pexels_url, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (photo["url"], photo["photographer"], photo["photographer_url"], photo["pexels_url"], now),
            )
        conn.commit()

    print(f"✅ 首頁 hero 輪播圖已更新，共 {len(photos)} 張（來自 Pexels）。")

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
        # 前端要知道「這則評論是不是我自己寫的」才能決定要不要顯示編輯／
        # 刪除按鈕，不能只靠「有沒有登入」判斷（登入了也不該能刪別人的
        # 評論的按鈕，雖然後端本來就會擋，但按鈕不該一開始就顯示出來）。
        "userId": row["user_id"],
        "contextTags": json.loads(row["context_tags"] or "[]"),
        # AI 從評論文字自動分析出來的標籤，跟上面使用者自己勾的分開存、
        # 分開回傳——前端要能分開顯示成不同樣式（見 extract_ai_review_tags()
        # 的註解）。
        "aiContextTags": json.loads(row["ai_context_tags"] or "[]"),
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
        # 情境類關鍵字：這批是「SugarTopia 不只是甜點版 Google Maps」的
        # 差異化重點（見主要開發目標），使用者常常不是直接說「甜點」，
        # 而是描述情境（想工作、想放空、怕太甜、想拍照），這些字本身沒有
        # 明顯的「甜點/店家」訊號，原本的關鍵字清單接不住，會被誤判成
        # out_of_scope。
        "插座", "不限時", "限時", "放空", "拍照", "網美", "一個人",
        "情侶", "生日", "聚會", "聊天", "甜度", "不太甜", "少糖",
        "寵物友善", "帶狗", "貓咖", "毛孩", "排隊",
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

# 獨立的 try/except，不要跟上面會員資料庫那段共用：hero 輪播圖是錦上添花
# 的功能，Pexels 出狀況不該讓 startup_error 被覆蓋掉、也不該影響會員
# 系統/店家資料是否成功載入的判斷。
try:
    refresh_hero_photos()
except Exception as e:
    print(f"⚠️ 首頁 hero 輪播圖更新失敗，沿用前端本地圖片：{e}")

try:
    if not GOOGLE_API_KEY:
        raise RuntimeError(
            "Missing Google Gemini API key. Set GOOGLE_API_KEY or GEMINI_API_KEY in your environment."
        )

    # 1. 建立 Embedding 模型
    embeddings = GoogleGenerativeAIEmbeddings(model=GEMINI_EMBEDDING_MODEL, google_api_key=GOOGLE_API_KEY)

    # 2. 讀取 dessert_data_sample.json 資料（7 家示意店家）
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

        # 2.5 curated_shops（Google Places 收錄的真實店家）也一起餵進向量
        # 資料庫，AI 才會知道這批真實店家存在。欄位跟示意資料不完全一樣
        # （curated_shops 沒有逐則評論文字，description 常常是空的——
        # admin_places.html 收錄流程目前沒有補這欄），格式盡量比照上面
        # 示意資料的寫法，缺的欄位用友善的預設文字頂著，不留空白段落。
        # 這裡只處理「開機當下已經存在」的 curated_shops。之後透過 admin
        # 收錄的新店家不會再走這個迴圈，而是由 add_shop_to_vector_db()
        # 在新增成功當下增量加進 vector_db（見下面該函式定義），不用等
        # 下次重新部署/重啟。
        for item in curated_shops:
            content = (
                f"店名：{item['nameZh'] or item['name']}\n"
                f"英文名稱：{item['name']}\n"
                f"分類：{item['categoryZh'] or item['category'] or '尚未分類'}\n"
                f"地點：{item['locationZh'] or item['location'] or '台北'}\n"
                f"評分：{item['rating'] if item['rating'] else '暫無評分'}\n"
                f"特色標籤：{', '.join(item['tagsZh'] or item['tags']) or '暫無標籤'}\n"
                f"介紹：{item['description'] or 'Google Places 收錄的真實台北店家，目前沒有額外的文字介紹。'}\n"
                f"資料來源：Google Places 收錄的真實店家"
            )
            documents.append(Document(page_content=content))

        # 3. 建立 Chroma 向量資料庫
        vector_db = Chroma.from_documents(documents, embeddings)
        vector_document_count = len(documents)
        print(f"✅ 甜點資料庫載入成功！（{len(dessert_data)} 家示意店家 + {len(curated_shops)} 家 Google Places 真實店家）")
    except Exception as e:
        raise RuntimeError(f"資料庫載入失敗，請確認檔案與 Gemini API key 是否正確。錯誤: {e}") from e

    # 4. 設定 Gemini 語言模型 (改用 LangChain 模組解決棄用警告)
    # timeout=15：原本沒設超時，Gemini 額度被限流／回應變慢時 llm.invoke()
    # 會卡住不回應，卡多久沒有上限——這是實測跑 Playwright 測試時真的
    # 踩到的（POST /api/shops/{id}/reviews 卡到前端 30 秒直接逾時失敗）。
    # 這支 llm 物件同時給 /api/chat 跟 extract_ai_review_tags() 共用，兩邊
    # 都受惠於同一個超時設定，不用各自補。
    llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL, google_api_key=GOOGLE_API_KEY, timeout=15)
except Exception as e:
    startup_error = str(e)
    print(f"❌ AI 服務初始化失敗: {startup_error}")

def add_shop_to_vector_db(shop):
    # AI 即時認識新店家：向量資料庫（vector_db）本來只在後端啟動時建立
    # 一次（見上面第 2.5 步的註解），所以透過 admin 收錄的新店家，AI
    # 問答完全不會知道，要等下一次重新部署重啟後端才會被餵進去。這支
    # 函式在店家新增成功「之後」呼叫，用 Chroma 的 add_documents() 把
    # 這一家店的資料直接增量加進現有的向量庫，不用整個重建，AI 馬上就
    # 能認得這家新店。
    #
    # 內容格式刻意跟開機時第 2.5 步 curated_shops 那段完全一樣，維持
    # AI 看到的資料格式一致（新店家在向量庫裡跟舊店家長得一樣，AI 不會
    # 特別區分「這家是後來加的」）。
    #
    # 失敗只印警告、不往外拋例外：新增店家本身已經成功、已經存進資料庫
    # 了，AI 認不認得這家店是次要的，不該讓這個失敗連帶讓整個新增店家
    # 的 API 回應失敗（跟 extract_ai_review_tags() 對 AI 失敗的處理方式
    # 是同一個原則）。
    global vector_document_count
    if vector_db is None:
        return
    try:
        content = (
            f"店名：{shop['nameZh'] or shop['name']}\n"
            f"英文名稱：{shop['name']}\n"
            f"分類：{shop['categoryZh'] or shop['category'] or '尚未分類'}\n"
            f"地點：{shop['locationZh'] or shop['location'] or '台北'}\n"
            f"評分：{shop['rating'] if shop['rating'] else '暫無評分'}\n"
            f"特色標籤：{', '.join(shop['tagsZh'] or shop['tags']) or '暫無標籤'}\n"
            f"介紹：{shop['description'] or 'Google Places 收錄的真實台北店家，目前沒有額外的文字介紹。'}\n"
            f"資料來源：Google Places 收錄的真實店家"
        )
        vector_db.add_documents([Document(page_content=content)])
        vector_document_count += 1
        print(f"✅ 新店家「{shop['nameZh'] or shop['name']}」已即時加入 AI 向量資料庫。")
    except Exception as e:
        print(f"⚠️ 新店家加入 AI 向量資料庫失敗（不影響店家本身已成功新增）：{e}")

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
        # SugarTopia_nuxt（Nuxt 重構版）本機開發用的 port。Nuxt 預設跑在
        # 3000，但那個 port 跟開發者本機的公司專案衝突，所以本機開發改用
        # 4000（仍保留 3000 這幾條，避免哪天又有人在別台機器上用預設 port
        # 開，不用因為這條規則被卡住）。
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://[::]:3000",
        "http://[::1]:3000",
        "http://127.0.0.1:4000",
        "http://localhost:4000",
        "http://[::]:4000",
        "http://[::1]:4000",
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

class WishlistRequest(BaseModel):
    text: str

class ReviewRequest(BaseModel):
    rating: int
    text: str
    # 情境標籤（Phase 4「情境式心得」）：使用者寫評論時順手複選幾個符合
    # 這次體驗的情境（適合工作、安靜、有插座……），非必填、預設空陣列。
    # 故意跟店家本身的 tags 用同一套「字串陣列」格式，不是另外設計一套
    # 結構——之後要做「AI 從評論內容自動長標籤」時，這批使用者自己選的
    # 標籤跟 AI 長出來的標籤才能直接合併，不用再做一次格式轉換。
    context_tags: list[str] = []

# 情境標籤固定字典，跟前端 write-review.vue 的選項一一對應。固定字典而
# 不是讓使用者自己打字：一方面避免亂七八糟的自由字串污染標籤池（拼字
# 不一致、語言混雜），另一方面這批標籤之後要真的拿去做篩選/搜尋比對，
# 選項必須是可控、可預期的固定集合才有用。
REVIEW_CONTEXT_TAGS = {
    "Work Friendly", "Quiet", "Outlets", "Solo Friendly", "Instagrammable", "Long Wait",
}

def clean_context_tags(tags):
    # 只留字典裡有的值，其餘（不管是打字打錯、還是有人直接打 API 亂塞）
    # 都直接丟掉，不回錯誤——寫評論的人不需要知道有這個字典存在，安靜
    # 過濾掉就好，不用因為傳了奇怪的值就擋掉整篇評論。
    return sorted({tag for tag in tags if tag in REVIEW_CONTEXT_TAGS})

# 給 Gemini 看的中文對照（後端這裡只有純 Python，不能借用前端 i18n
# 語言檔），只是幫助模型理解語意用，不是存進資料庫的欄位。
REVIEW_CONTEXT_TAG_LABELS_ZH = {
    "Work Friendly": "適合工作",
    "Quiet": "安靜",
    "Outlets": "有插座",
    "Solo Friendly": "適合一個人",
    "Instagrammable": "拍照好看",
    "Long Wait": "排隊要等一下",
}

def extract_ai_review_tags(review_text, existing_tags):
    # Phase 4「AI 標籤整理」：從評論文字裡自動抓情境標籤，不用使用者自己
    # 全部手動勾選。只補使用者「還沒勾」的部分（existing_tags 是使用者
    # 自己已經選的），避免重複判斷使用者已經明確告訴我們的資訊。
    #
    # 這支函式失敗（llm 還沒 ready、Gemini 額度用完、回傳格式解析不出來）
    # 都直接回空陣列，不拋例外——AI 標籤是評論功能的加分項，不是評論
    # 送出去的必要條件，AI 那邊出問題不該連帶讓使用者評論送不出去。
    if llm is None or not review_text.strip():
        return []

    tag_list = ", ".join(REVIEW_CONTEXT_TAGS)
    zh_hints = "\n".join(f"{en} = {REVIEW_CONTEXT_TAG_LABELS_ZH[en]}" for en in sorted(REVIEW_CONTEXT_TAGS))
    existing_line = ", ".join(existing_tags) if existing_tags else "（沒有）"

    prompt = f"""
    你是在幫忙從使用者寫的甜點店評論裡，抓出符合的情境標籤。

    只能從這個固定清單裡選，不可以自己發明新的標籤：
    {tag_list}

    這些標籤的中文對照，方便你理解語意：
    {zh_hints}

    使用者已經自己勾選了這些標籤，不要重複輸出：
    {existing_line}

    規則：
    - 只有評論文字裡明確、清楚支持的標籤才能選，不要用一般常識腦補
      （例如評論完全沒提到插座、座位，不能因為「是咖啡廳」就假設有插座）。
    - 沒有任何符合的就回傳空陣列，不要為了有結果硬選。
    - 只能輸出一個 JSON 陣列（例如 ["Quiet", "Long Wait"]），不要有其他
      文字說明，不要用 markdown code fence。

    評論文字：
    {review_text}
    """

    try:
        response = llm.invoke(prompt)
        # response.content 不保證一定是字串（有時候是一段結構化的 list/dict），
        # 跟 /api/chat 那邊遇到的狀況一樣，直接借用同一支 format_model_content()
        # 轉成純文字，不要自己重寫一次同樣的邏輯。
        content = format_model_content(response.content).strip()
        # 保守處理：即使叮嚀了不要用 code fence，Gemini 偶爾還是會包一層
        # ```json ... ```，這裡順手拆掉，不要因為這種小狀況整段解析失敗。
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            return []
        # clean_context_tags() 已經擋掉字典以外的值；這裡再扣掉使用者自己
        # 已經勾過的（提示詞裡也要求過，這是雙重保險，不完全依賴模型
        # 一定會照規則做）。
        ai_tags = clean_context_tags(str(tag) for tag in parsed)
        return sorted(set(ai_tags) - set(existing_tags))
    except Exception as e:
        print(f"⚠️ AI 標籤分析失敗，忽略、不擋評論送出：{e}")
        return []

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
        "database": "postgres" if DATABASE_URL else "not configured",
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
                RETURNING id
                """,
                (name, email, hash_password(password), now),
            )
            user_id = cursor.fetchone()["id"]
            conn.commit()
            user = conn.execute(
                "SELECT id, name, email, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except psycopg2.errors.UniqueViolation:
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

@app.get("/api/hero-photos")
def get_hero_photos():
    # 公開端點，不用登入——這批照片本來就是首頁公開顯示用的，跟店家
    # 資料一樣的開放程度。
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM hero_photos ORDER BY id ASC").fetchall()

    return {
        "photos": [
            {
                "url": row["url"],
                "photographer": row["photographer"],
                "photographerUrl": row["photographer_url"],
                "pexelsUrl": row["pexels_url"],
            }
            for row in rows
        ],
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

@app.get("/api/wishlist")
def get_wishlist(authorization: str = Header(default="")):
    user = require_current_user(authorization)

    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, text, created_at FROM wishlist WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()

    return {
        "total": len(rows),
        "items": [{"id": row["id"], "text": row["text"], "createdAt": row["created_at"]} for row in rows],
    }

@app.post("/api/wishlist")
def add_wishlist_item(request: WishlistRequest, authorization: str = Header(default="")):
    user = require_current_user(authorization)

    text = request.text.strip()
    if len(text) < 2:
        raise HTTPException(status_code=400, detail="Wishlist text is too short.")

    now = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO wishlist (user_id, text, created_at) VALUES (?, ?, ?) RETURNING id",
            (user["id"], text, now),
        )
        item_id = cursor.fetchone()["id"]
        conn.commit()

    return {"id": item_id, "text": text, "createdAt": now}

@app.delete("/api/wishlist/{item_id}")
def remove_wishlist_item(item_id: int, authorization: str = Header(default="")):
    user = require_current_user(authorization)

    with get_db_connection() as conn:
        # 跟收藏／評論同一種「WHERE 條件裡帶上 user_id」的做法，靠資料庫
        # 本身擋掉別人的資料——這裡不像評論那樣需要另外查一次來區分
        # 404／403，因為願望單本來就沒有「看得到但不能刪」這種中間狀態，
        # 不是自己的就等於查無此筆，統一都是「沒東西可以刪」，回應一樣。
        conn.execute(
            "DELETE FROM wishlist WHERE id = ? AND user_id = ?",
            (item_id, user["id"]),
        )
        conn.commit()

    return {"message": "Wishlist item deleted.", "id": item_id}

@app.get("/api/shops/{shop_id}/reviews")
def get_shop_reviews(shop_id: str):
    if find_shop(shop_id) is None:
        raise HTTPException(status_code=404, detail="Shop not found.")

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT reviews.id, reviews.shop_id, reviews.rating, reviews.review_text, reviews.created_at, reviews.user_id, reviews.context_tags, reviews.ai_context_tags, users.name
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
    context_tags = clean_context_tags(request.context_tags)
    # 同步呼叫 Gemini（不是背景任務）：評論不是每分鐘都有人在寫，量不大，
    # 先用最簡單的做法，不用一開始就搞非同步機制。AI 失敗不會擋評論送出
    # （見 extract_ai_review_tags() 內部的例外處理），最壞情況只是這則
    # 評論沒有 AI 標籤，不影響評論本身送出成功。
    ai_context_tags = extract_ai_review_tags(review_text, context_tags)

    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reviews (user_id, shop_id, rating, review_text, created_at, context_tags, ai_context_tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (user["id"], shop_id, request.rating, review_text, now, json.dumps(context_tags), json.dumps(ai_context_tags)),
        )
        review_id = cursor.fetchone()["id"]
        conn.commit()
        row = conn.execute(
            """
            SELECT reviews.id, reviews.shop_id, reviews.rating, reviews.review_text, reviews.created_at, reviews.user_id, reviews.context_tags, reviews.ai_context_tags, users.name
            FROM reviews
            JOIN users ON users.id = reviews.user_id
            WHERE reviews.id = ?
            """,
            (review_id,),
        ).fetchone()

    return serialize_review(row)

@app.put("/api/reviews/{review_id}")
def update_review(review_id: int, request: ReviewRequest, authorization: str = Header(default="")):
    user = require_current_user(authorization)

    if request.rating < 1 or request.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")

    review_text = request.text.strip()
    if len(review_text) < 2:
        raise HTTPException(status_code=400, detail="Review text is too short.")

    with get_db_connection() as conn:
        existing = conn.execute("SELECT user_id FROM reviews WHERE id = ?", (review_id,)).fetchone()

        if existing is None:
            raise HTTPException(status_code=404, detail="Review not found.")

        # 只有原作者能改自己的評論，不看是不是登入就能改任何一則——跟收藏
        # 的 DELETE FROM favorites WHERE user_id = ? AND shop_id = ? 是同一個
        # 「WHERE 條件裡帶上 user_id，靠資料庫本身擋掉別人的資料」的做法，
        # 但這裡多了 review_text／rating 這種使用者自己輸入的內容，光靠
        # WHERE 條件式的 UPDATE 静默失敗（改到 0 筆）不夠清楚，所以先查一次
        # 現有資料的 user_id，不是自己的就明確擋掉、回 403，而不是讓請求
        # 看起來「成功」但其實什麼都沒改到。
        if existing["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="You can only edit your own reviews.")

        context_tags = clean_context_tags(request.context_tags)
        # 編輯評論也重新跑一次 AI 分析：文字內容可能整個改過，舊的 AI
        # 標籤不見得還準；重新分析一次成本不高（評論編輯本來就不頻繁）。
        ai_context_tags = extract_ai_review_tags(review_text, context_tags)
        conn.execute(
            "UPDATE reviews SET rating = ?, review_text = ?, context_tags = ?, ai_context_tags = ? WHERE id = ?",
            (request.rating, review_text, json.dumps(context_tags), json.dumps(ai_context_tags), review_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT reviews.id, reviews.shop_id, reviews.rating, reviews.review_text, reviews.created_at, reviews.user_id, reviews.context_tags, reviews.ai_context_tags, users.name
            FROM reviews
            JOIN users ON users.id = reviews.user_id
            WHERE reviews.id = ?
            """,
            (review_id,),
        ).fetchone()

    return serialize_review(row)

@app.delete("/api/reviews/{review_id}")
def delete_review(review_id: int, authorization: str = Header(default="")):
    user = require_current_user(authorization)

    with get_db_connection() as conn:
        existing = conn.execute("SELECT user_id FROM reviews WHERE id = ?", (review_id,)).fetchone()

        if existing is None:
            raise HTTPException(status_code=404, detail="Review not found.")

        if existing["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="You can only delete your own reviews.")

        conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        conn.commit()

    return {"message": "Review deleted.", "reviewId": review_id}

@app.get("/api/reviews/latest")
def get_latest_reviews(limit: int = 8):
    capped_limit = max(1, min(limit, 20))

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT reviews.id, reviews.shop_id, reviews.rating, reviews.review_text, reviews.created_at, reviews.user_id, reviews.context_tags, reviews.ai_context_tags, users.name
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
        review["shopNameZh"] = shop["nameZh"] if shop else row["shop_id"]
        review["shopImage"] = shop["image"] if shop else ""
        review_list.append(review)

    return {
        "total": len(review_list),
        "reviews": review_list,
    }

@app.get("/api/google/places/search")
def search_google_places(q: str = "", authorization: str = Header(default="")):
    require_admin_user(authorization)

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
def get_google_place(place_id: str, authorization: str = Header(default="")):
    require_admin_user(authorization)

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

# curated_shops 的 image 欄位存的是「/api/places/photo?name=...」這種指到
# 自己這支路由的網址（見 add_curated_shop() 怎麼組出這個網址），不是
# Google 原始的照片網址。原因：Google Places 的照片要帶 API key 才能存取，
# 如果直接把 Google 的照片網址（帶 key）存進資料庫、讓前端 <img src> 直接
# 指過去，等於把後端的 API key 整個曝光在瀏覽器看得到的網頁原始碼裡，
# 任何人都能複製去用、算在你的 Google 帳單額度裡。改成這支後端自己的路由
# 幫忙代理：前端只看得到這支路由的網址，真正帶 key 去跟 Google 要照片、
# 再把照片內容原封不動轉給前端的動作，都在後端做，key 不會外流。
#
# 沒有掛 require_admin_user：這支路由本身不會新增/修改任何資料，只是把
# 「本來就已經透過 GET /api/shops 公開給所有人看」的店家照片轉出去，跟
# 站內其他公開圖片（/img/*.jpg 這些）是同一個開放程度，不用額外限制。
@app.get("/api/places/photo")
def get_places_photo(name: str, max_width: int = 800):
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=503, detail="Google Places API key is not configured.")

    try:
        response = requests.get(
            f"{GOOGLE_PLACES_BASE_URL}/{name}/media",
            params={"maxWidthPx": max_width, "key": GOOGLE_PLACES_API_KEY},
            timeout=10,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach Google Places API.")

    if not response.ok:
        raise HTTPException(status_code=502, detail="Could not load photo from Google Places.")

    return Response(
        content=response.content,
        media_type=response.headers.get("Content-Type", "image/jpeg"),
        # 瀏覽器／CDN 快取一天：店家照片幾乎不會變，沒必要每次都重新跟
        # Google 要一次（Google Places Photo API 本身也是有配額限制的）。
        headers={"Cache-Control": "public, max-age=86400"},
    )

@app.post("/api/shops/curated")
def add_curated_shop(request: CuratedShopRequest, authorization: str = Header(default="")):
    require_admin_user(authorization)

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
                    "googleMapsUri,location,photos"
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

    # 只拿第一張照片（Google 通常會回好幾張，這裡不用全部收，一張封面
    # 照就夠了，跟現有 7 家示意店家一家一張圖是同一個做法）。photos[i].name
    # 是 Google 那邊的照片資源路徑（例如 "places/ABC/photos/XYZ"），不是
    # 可以直接用的網址，要透過 /api/places/photo 這支自己的路由代理出去
    # （原因見那支路由的註解——直接存 Google 的照片網址會外流 API key）。
    photos = place.get("photos", [])
    image_url = ""
    if photos and photos[0].get("name"):
        image_url = f"{PUBLIC_BASE_URL}/api/places/photo?name={quote(photos[0]['name'], safe='')}"

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
                image_url,
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
    add_shop_to_vector_db(new_shop)

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
            search_results = vector_db.similarity_search(request.message, k=min(4, vector_document_count))

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

            推薦原因請具體對應到使用者問題裡的情境需求，而不是只複述店名跟分類。
            參考資料裡每家店的「特色標籤」欄位（例如：適合工作、安靜、有插座、
            不限時、寵物友善、貓咖、酒香甜點、網美、下午茶）就是這類情境線索，
            回答時明確點出符合使用者需求的標籤，例如使用者問「想找可以工作的
            咖啡廳」，回答要具體說「這家有插座、環境安靜，適合久坐工作」，
            不要只說「這家不錯」。這是 SugarTopia 想做到「懂使用者情境」、
            不只是甜點版 Google Map 的重點，請認真對應，不要隨便帶過。

            如果只有部分符合，也可以清楚說明「目前 SugarTopia 資料中最接近的是...」，
            並誠實說明哪個情境條件沒有完全符合。
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

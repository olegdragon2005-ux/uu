import asyncio
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

ROOT = Path(__file__).parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "data" / "mesh_orders.sqlite3"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "").strip()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mesh")

bot_app = None
rate_limit = {}
RATE_WINDOW = 60
RATE_MAX = 5

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        customer TEXT, contact TEXT, comment TEXT, source_url TEXT,
        page_url TEXT, model_json TEXT, status TEXT DEFAULT 'new'
    )""")
    conn.commit()
    return conn

class ModelInfo(BaseModel):
    id: int | str | None = None
    name: str = Field(default="Модель не выбрана", max_length=180)
    category: str | None = Field(default=None, max_length=80)
    format: str | None = Field(default=None, max_length=20)
    size: int | float | str | None = None

class OrderIn(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    contact: str = Field(min_length=1, max_length=100)
    comment: str = Field(default="", max_length=1000)
    source_url: str = Field(default="", max_length=500)
    page_url: str = Field(default="", max_length=1000)
    model: ModelInfo | None = None
    website: str = Field(default="", max_length=200)  # honeypot

def site_search_links(name: str):
    q = quote_plus(name)
    return [
        ("MakerWorld", f"https://makerworld.com/en/search/models?keyword={q}"),
        ("Printables", f"https://www.printables.com/search/models?q={q}"),
    ]

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "MESH 3D Orders активен.\n"
        f"ID этого чата: {chat_id}\n\n"
        "Чтобы получать заказы в этот чат, добавьте число выше в переменную "
        "ADMIN_CHAT_ID в настройках Render и перезапустите сервис.\n"
        "Команды: /id — показать ID чата, /help — справка."
    )

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот принимает заказы с сайта MESH и пересылает их администратору.\n"
        "Для настройки администратора используйте /id и переменную ADMIN_CHAT_ID в Render."
    )

def build_bot():
    if not BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not configured; Telegram polling is disabled.")
        return None
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    return app

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    bot_app = build_bot()
    if bot_app:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram polling started")
    yield
    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()

app = FastAPI(title="MESH Orders API", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok", "telegram_configured": bool(BOT_TOKEN),
            "admin_configured": bool(ADMIN_CHAT_ID)}

@app.get("/")
async def home():
    return FileResponse(ROOT / "index.html")

@app.post("/api/orders")
async def create_order(data: OrderIn, request: Request):
    # Silent honeypot for basic spam filtering.
    if data.website:
        return {"ok": True, "order_id": "received"}
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in rate_limit.get(ip, []) if now - t < RATE_WINDOW]
    if len(recent) >= RATE_MAX:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте через минуту.")
    recent.append(now)
    rate_limit[ip] = recent

    if not BOT_TOKEN or not bot_app:
        raise HTTPException(status_code=503, detail="Бот ещё не настроен. Попробуйте позже.")
    if not ADMIN_CHAT_ID:
        raise HTTPException(status_code=503, detail="Администратор ещё не подключил Telegram-чат.")

    if data.source_url and not re.match(
        r"^https://(www\.)?(makerworld\.com|printables\.com)/", data.source_url, re.I
    ):
        raise HTTPException(status_code=400, detail="Разрешены ссылки только на MakerWorld и Printables.")

    order_id = uuid.uuid4().hex[:8].upper()
    model = data.model
    model_name = model.name if model else "Не выбрана"
    lines = [
        f"🧾 НОВЫЙ ЗАКАЗ MESH №{order_id}",
        f"👤 Клиент: {data.customer}",
        f"📞 Контакт: {data.contact}",
        f"🧩 Модель: {model_name}",
    ]
    if model and model.category:
        lines.append(f"Категория: {model.category}")
    if model and model.format:
        lines.append(f"Формат в каталоге: {model.format}")
    if model and model.id is not None:
        lines.append(f"ID модели MESH: {model.id}")
    if data.source_url:
        lines.append(f"🔗 Ссылка клиента: {data.source_url}")
    if data.comment:
        lines.append(f"💬 Комментарий: {data.comment}")
    lines.append(f"🌐 Страница заказа: {data.page_url or PUBLIC_SITE_URL or 'не указана'}")

    keyboard = []
    if data.source_url:
        keyboard.append([InlineKeyboardButton("Открыть ссылку модели", url=data.source_url)])
    elif model:
        for title, url in site_search_links(model_name):
            keyboard.append([InlineKeyboardButton(f"Искать на {title}", url=url)])
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    conn = db()
    import json
    conn.execute(
        "INSERT INTO orders (id, customer, contact, comment, source_url, page_url, model_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, data.customer, data.contact, data.comment, data.source_url,
         data.page_url, json.dumps(model.model_dump() if model else None, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

    try:
        await bot_app.bot.send_message(
            chat_id=int(ADMIN_CHAT_ID),
            text="\n".join(lines),
            reply_markup=markup,
            disable_web_page_preview=True
        )
    except Exception:
        log.exception("Failed to deliver order %s to admin chat", order_id)
        raise HTTPException(status_code=502, detail="Заказ сохранён, но Telegram не принял сообщение. Администратору нужно проверить настройки.")
    return {"ok": True, "order_id": order_id}

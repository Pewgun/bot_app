import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
import psycopg2

# 1. Configuration
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN = os.getenv("RAILWAY_STATIC_URL") # Provided by Railway
PORT = int(os.getenv("PORT", 3000))

app = FastAPI()
# Initialize the Telegram Application
ptb_app = ApplicationBuilder().token(TOKEN).build()

# 2. Database Logic
def save_to_db(username, text):
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            print(f"DEBUG - text: {text}")
            cur.execute("INSERT INTO messages (username, content) VALUES (%s, %s)", (username, text))
            conn.commit()

# 3. Bot Logic
async def handle_message(update, context):
    user = update.message.from_user.username or "Anonymous"
    text = update.message.text
    save_to_db(user, text)

ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 4. Webhook Route
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await ptb_app.update_queue.put(Update.de_json(data, ptb_app.bot))
    return {"status": "ok"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    webhook_url = f"https://{DOMAIN}/webhook"
    await ptb_app.initialize()
    await ptb_app.bot.set_webhook(url=webhook_url)
    await ptb_app.start()
    
    yield  # The application runs while this sits here
    
    # --- Shutdown Logic ---
    await ptb_app.stop()
    await ptb_app.shutdown()

# 5. Startup: Tell Telegram where to send updates
"""@app.on_event("startup")
async def on_startup():
    webhook_url = f"https://{DOMAIN}/webhook"
    await ptb_app.initialize()
    await ptb_app.bot.set_webhook(url=webhook_url)
    await ptb_app.start()
"""
"""
import os
import asyncio
import psycopg2
from aiogram import Bot, Dispatcher, types

# Railway provides DATABASE_URL automatically if you link the services
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Database setup
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            user_name TEXT,
            chat_id BIGINT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

# Catch-all handler for EVERY message
@dp.message()
async def save_message(message: types.Message):
    if message.text: # Only save text messages
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (user_name, chat_id, content) VALUES (%s, %s, %s)",
            (message.from_user.full_name, message.chat.id, message.text)
        )
        conn.commit()
        cur.close()
        conn.close()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
"""

import os
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
import psycopg2

# 1. Configuration
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN = os.getenv("RAILWAY_STATIC_URL") # Provided by Railway
PORT = int(os.getenv("PORT", 8080))

app = FastAPI()
# Initialize the Telegram Application
ptb_app = ApplicationBuilder().token(TOKEN).build()

# 2. Database Logic
def save_to_db(username, text):
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
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

# 5. Startup: Tell Telegram where to send updates
@app.on_event("startup")
async def on_startup():
    webhook_url = f"https://{DOMAIN}/webhook"
    await ptb_app.initialize()
    await ptb_app.bot.set_webhook(url=webhook_url)
    await ptb_app.start()
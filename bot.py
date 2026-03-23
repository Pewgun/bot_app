import os
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# 1. Configuration
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN = os.getenv("RAILWAY_STATIC_URL") # Provided by Railway
PORT = int(os.getenv("PORT", 3000))

# Initialize the Telegram Application
ptb_app = ApplicationBuilder().token(TOKEN).build()

# 2. Database Logic
async def init_db():
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        username TEXT,
                        content TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            conn.commit()
        logging.info("Database initialized: 'messages' table is ready.")
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")

def save_to_db(username, text):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO messages (username, content) VALUES (%s, %s)", (username, text))
            conn.commit()
        logging.info(f"Message from {username} saved to database successfully.")
    except Exception as e:
        logging.error(f"Failed to save message to database: {e}")

# 3. Bot Logic
async def handle_message(update, context):
    user = update.message.from_user.username or "Anonymous"
    text = update.message.text
    try:
        save_to_db(user, text)
    except Exception as e:
        logging.error(f"Error in handle_message while saving to database: {e}")

ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 4. Lifespan: startup and shutdown logic
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    await init_db()
    webhook_url = f"https://{DOMAIN}/webhook"
    await ptb_app.initialize()
    await ptb_app.bot.set_webhook(url=webhook_url)
    await ptb_app.start()

    yield  # The application runs while this sits here

    # --- Shutdown Logic ---
    await ptb_app.stop()
    await ptb_app.shutdown()

# 5. FastAPI app (defined after lifespan so it can be passed as a parameter)
app = FastAPI(lifespan=lifespan)

# 6. Webhook Route
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await ptb_app.update_queue.put(Update.de_json(data, ptb_app.bot))
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

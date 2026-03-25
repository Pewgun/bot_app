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
                # Set the session timezone so that NOW() returns Europe/Riga
                # time and naive timestamps are interpreted correctly during
                # migration.
                cur.execute("SET timezone = 'Europe/Riga'")
                #cur.execute("ALTER DATABASE railway SET timezone TO 'Europe/Riga'")

                # Check whether the messages table already exists
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                        AND table_name = 'messages'
                    )
                """)
                table_exists = cur.fetchone()[0]

                if table_exists:
                    # Check if the existing table is missing timezone support on created_at.
                    # We detect this by looking at the data_type of the column.
                    cur.execute("""
                        SELECT data_type FROM information_schema.columns
                        WHERE table_schema = 'public'
                        AND table_name = 'messages'
                        AND column_name = 'created_at'
                    """)
                    row = cur.fetchone()
                    created_at_type = row[0] if row else None

                    if created_at_type != 'timestamp with time zone':
                        # Old schema detected — migrate data into a new table with the
                        # correct schema, then swap it in place of the old one.
                        logging.info("Old 'messages' schema detected. Starting safe migration...")

                        cur.execute("""
                            CREATE TABLE messages_new (
                                id SERIAL PRIMARY KEY,
                                username TEXT,
                                content TEXT,
                                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                            )
                        """)

                        # Copy all existing rows; treat the old timestamps as
                        # Europe/Riga local time when converting to timestamptz.
                        cur.execute("""
                            INSERT INTO messages_new (id, username, content, created_at)
                            SELECT id, username, content, 
                                created_at AT TIME ZONE 'Europe/Riga'
                            FROM messages
                        """)

                        # AT TIME ZONE 'Europe/Riga'

                        # Carry the sequence forward so future inserts don't collide.
                        cur.execute("""
                            SELECT setval(
                                pg_get_serial_sequence('messages_new', 'id'),
                                COALESCE((SELECT MAX(id) FROM messages_new), 1)
                            )
                        """)

                        cur.execute("DROP TABLE messages")
                        cur.execute("ALTER TABLE messages_new RENAME TO messages")

                        # Changing time from utc to Latvia .
                        
                        cur.execute("UPDATE messages SET created_at = created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Riga'")

                        logging.info("Migration complete: all old messages preserved with timezone-aware timestamps.")
                    else:
                        logging.info("'messages' table already has the correct schema. No migration needed.")
                else:
                    # Fresh install — create the table with the correct schema from the start.
                    cur.execute("""
                        CREATE TABLE messages (
                            id SERIAL PRIMARY KEY,
                            username TEXT,
                            content TEXT,
                            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                        )
                    """)
                    logging.info("'messages' table created with timezone-aware schema.")

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

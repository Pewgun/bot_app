import os
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
import psycopg2
import psycopg2.extras
from openai import OpenAI
from openai import AuthenticationError, RateLimitError, OpenAIError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# 1. Configuration
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN = os.getenv("RAILWAY_STATIC_URL") # Provided by Railway
PORT = int(os.getenv("PORT", 3000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

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
                                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                                group_chat_id BIGINT,
                                group_chat_title TEXT
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

                        logging.info("Migration complete: all old messages preserved with timezone-aware timestamps.")
                    else:
                        # Table has the correct schema — ensure the group_chat_id and
                        # group_chat_title columns exist (added in a later migration).
                        cur.execute("""
                            ALTER TABLE messages
                                ADD COLUMN IF NOT EXISTS group_chat_id BIGINT,
                                ADD COLUMN IF NOT EXISTS group_chat_title TEXT
                        """)
                        logging.info("'messages' table already has the correct schema. No migration needed.")
                else:
                    # Fresh install — create the table with the correct schema from the start.
                    cur.execute("""
                        CREATE TABLE messages (
                            id SERIAL PRIMARY KEY,
                            username TEXT,
                            content TEXT,
                            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                            group_chat_id BIGINT,
                            group_chat_title TEXT
                        )
                    """)
                    logging.info("'messages' table created with timezone-aware schema.")

            conn.commit()
        logging.info("Database initialized: 'messages' table is ready.")
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")

def save_to_db(username, text, chat_id=None, chat_title=None):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (username, content, group_chat_id, group_chat_title) VALUES (%s, %s, %s, %s)",
                    (username, text, chat_id, chat_title)
                )
                conn.commit()
        logging.info(f"Message from {username} saved to database successfully.")
    except Exception as e:
        logging.error(f"Failed to save message to database: {e}")

# 3. Bot Logic
async def handle_message(update, context):
    user = update.message.from_user.username or "Anonymous"
    text = update.message.text
    chat_id = update.message.chat.id if update.message.chat else None
    chat_title = update.message.chat.title if update.message.chat else None
    try:
        save_to_db(user, text, chat_id=chat_id, chat_title=chat_title)
    except Exception as e:
        logging.error(f"Error in handle_message while saving to database: {e}")

ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 4. Request/Response Models
class AnalyzeRequest(BaseModel):
    messages: List[dict]
    prompt: str

class SearchRequest(BaseModel):
    query: str

# 5. Lifespan: startup and shutdown logic
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

# 6. FastAPI app (defined after lifespan so it can be passed as a parameter)
app = FastAPI(lifespan=lifespan)

# 7. CORS Middleware — allow any origin so the React frontend can call these endpoints
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 8. Webhook Route
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await ptb_app.update_queue.put(Update.de_json(data, ptb_app.bot))
    return {"status": "ok"}

# 9. REST API Routes

@app.get("/api/messages")
async def get_messages(
    group_id: Optional[int] = Query(default=None, description="Filter by group_chat_id"),
    limit: int = Query(default=100, ge=1, le=1000, description="Number of messages to return"),
    offset: int = Query(default=0, ge=0, description="Number of messages to skip"),
):
    """Fetch messages from the database with optional group filter and pagination."""
    logging.info(f"GET /api/messages — group_id={group_id}, limit={limit}, offset={offset}")
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if group_id is not None:
                    cur.execute(
                        """
                        SELECT id, username, content, created_at, group_chat_id, group_chat_title
                        FROM messages
                        WHERE group_chat_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s OFFSET %s
                        """,
                        (group_id, limit, offset),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, username, content, created_at, group_chat_id, group_chat_title
                        FROM messages
                        ORDER BY created_at DESC
                        LIMIT %s OFFSET %s
                        """,
                        (limit, offset),
                    )
                rows = cur.fetchall()
        # Convert rows to plain dicts and serialise datetime objects to ISO strings
        messages = [
            {
                **dict(row),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]
        logging.info(f"Returning {len(messages)} messages")
        return JSONResponse(content=messages)
    except Exception as e:
        logging.error(f"GET /api/messages error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


@app.get("/api/groups")
async def get_groups():
    """Return the list of unique groups that have messages in the database."""
    logging.info("GET /api/groups")
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT DISTINCT group_chat_id AS id, group_chat_title AS title
                    FROM messages
                    WHERE group_chat_id IS NOT NULL
                    ORDER BY group_chat_title
                    """
                )
                rows = cur.fetchall()
        groups = [dict(row) for row in rows]
        logging.info(f"Returning {len(groups)} groups")
        return JSONResponse(content=groups)
    except Exception as e:
        logging.error(f"GET /api/groups error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch groups")


@app.post("/api/ai/analyze")
async def ai_analyze(body: AnalyzeRequest):
    """Send a list of messages to OpenAI and return an analysis."""
    logging.info(f"POST /api/ai/analyze — {len(body.messages)} messages, prompt length={len(body.prompt)}")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured")
    try:
        # Build a readable transcript from the supplied messages
        transcript_lines = []
        for msg in body.messages:
            username = msg.get("username", "Unknown")
            content = msg.get("content", "")
            created_at = msg.get("created_at", "")
            transcript_lines.append(f"[{created_at}] {username}: {content}")
        transcript = "\n".join(transcript_lines)

        system_prompt = (
            "You are a helpful assistant that analyses Telegram group chat messages. "
            "Answer concisely and in the same language as the messages when possible."
        )
        user_message = f"{body.prompt}\n\nMessages:\n{transcript}"

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.3,
        )
        analysis = response.choices[0].message.content
        logging.info("OpenAI analysis completed successfully")
        return JSONResponse(content={"analysis": analysis})
    except AuthenticationError:
        logging.error("OpenAI authentication failed — check OPENAI_API_KEY")
        raise HTTPException(status_code=503, detail="OpenAI authentication failed")
    except RateLimitError:
        logging.error("OpenAI rate limit exceeded")
        raise HTTPException(status_code=429, detail="OpenAI rate limit exceeded — try again later")
    except OpenAIError as e:
        logging.error(f"OpenAI API error: {e}")
        raise HTTPException(status_code=502, detail=f"OpenAI API error: {str(e)}")
    except Exception as e:
        logging.error(f"POST /api/ai/analyze unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Failed to analyse messages")


@app.post("/api/ai/search")
async def ai_search(body: SearchRequest):
    """Search messages using a natural-language query and return matching rows plus an AI summary."""
    logging.info(f"POST /api/ai/search — query='{body.query}'")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured")
    try:
        # Fetch recent messages to search through (cap at 500 to stay within token limits)
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, username, content, created_at, group_chat_id, group_chat_title
                    FROM messages
                    ORDER BY created_at DESC
                    LIMIT 500
                    """
                )
                rows = cur.fetchall()

        messages = [
            {
                **dict(row),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]

        # Build a compact transcript for the model
        transcript_lines = []
        for msg in messages:
            transcript_lines.append(
                f"[id={msg['id']}][{msg['created_at']}] {msg['username']}: {msg['content']}"
            )
        transcript = "\n".join(transcript_lines)

        system_prompt = (
            "You are a search assistant for a Telegram group chat archive. "
            "Given a list of messages and a search query, identify the most relevant messages "
            "and return their IDs as a JSON array under the key 'ids', followed by a short "
            "human-readable summary under the key 'summary'. "
            "Respond ONLY with valid JSON, e.g.: {\"ids\": [1, 2, 3], \"summary\": \"...\"}"
        )
        user_message = f"Search query: {body.query}\n\nMessages:\n{transcript}"

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.2,
        )
        raw = response.choices[0].message.content
        logging.info(f"OpenAI search raw response: {raw}")

        import json as _json
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError:
            logging.warning("Could not parse OpenAI JSON response; returning raw text as summary")
            parsed = {"ids": [], "summary": raw}

        matched_ids = set(parsed.get("ids", []))
        results = [m for m in messages if m["id"] in matched_ids]
        summary = parsed.get("summary", "")

        logging.info(f"Search returned {len(results)} results")
        return JSONResponse(content={"results": results, "summary": summary})
    except AuthenticationError:
        logging.error("OpenAI authentication failed — check OPENAI_API_KEY")
        raise HTTPException(status_code=503, detail="OpenAI authentication failed")
    except RateLimitError:
        logging.error("OpenAI rate limit exceeded")
        raise HTTPException(status_code=429, detail="OpenAI rate limit exceeded — try again later")
    except OpenAIError as e:
        logging.error(f"OpenAI API error: {e}")
        raise HTTPException(status_code=502, detail=f"OpenAI API error: {str(e)}")
    except Exception as e:
        logging.error(f"POST /api/ai/search unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Failed to search messages")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

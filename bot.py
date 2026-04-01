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
#import google.generativeai as genai  # Add this
from google import genai  # Correct import for google-genai package
from google.genai import types

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
#gemini_model = genai.GenerativeModel("gemini-1.5-flash")
gemini_model = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

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

                # Create conversations and conversation_messages tables (idempotent)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id SERIAL PRIMARY KEY,
                        title VARCHAR(255),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_messages (
                        id SERIAL PRIMARY KEY,
                        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                        role VARCHAR(50) NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logging.info("'conversations' and 'conversation_messages' tables are ready.")

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

class ConversationCreate(BaseModel):
    title: Optional[str] = None

class ConversationUpdate(BaseModel):
    title: str

class MessageCreate(BaseModel):
    content: str

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

@app.post("/api/ai/gemanalyze")
async def ai_analyze(body: AnalyzeRequest):
    """Send a list of messages to Google Gemini and return an analysis."""
    logging.info(f"POST /api/ai/gemanalyze — {len(body.messages)} messages")
    
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini API key is not configured")
        
    try:
        # Build the transcript
        transcript_lines = []
        for msg in body.messages:
            username = msg.get("username", "Unknown")
            content = msg.get("content", "")
            created_at = msg.get("created_at", "")
            transcript_lines.append(f"[{created_at}] {username}: {content}")
        transcript = "\n".join(transcript_lines)

        # Gemini uses a single prompt string or a list of parts
        full_prompt = (
            "You are a helpful assistant that analyses Telegram group chat messages. "
            "Answer concisely and in the same language as the messages when possible.\n\n"
            f"User Task: {body.prompt}\n\n"
            f"Messages Transcript:\n{transcript}"
        )

        # Generate content
        response = gemini_model.models.generate_content(
            #model="gemini-2.0-flash",
            #model="Gemini 2.5 Flash-Lite",
            model="gemini-2.5-flash-lite",
            contents=full_prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                # max_output_tokens=500, # Optional safety cap
            )
        )
        
        analysis = response.text
        logging.info("Gemini analysis completed successfully")
        return JSONResponse(content={"analysis": analysis})

    except Exception as e:
        logging.error(f"Gemini API error: {e}")
        # Google's library raises standard Exceptions, but we can catch 429 specifically if needed
        if "429" in str(e):
             raise HTTPException(status_code=429, detail="Gemini rate limit exceeded")
        raise HTTPException(status_code=500, detail=f"Failed to analyze messages: {str(e)}")

@app.post("/api/ai/gptanalyze")
async def ai_analyze(body: AnalyzeRequest):
    """Send a list of messages to OpenAI and return an analysis."""
    logging.info(f"POST /api/ai/gptanalyze — {len(body.messages)} messages, prompt length={len(body.prompt)}")
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
            model="gpt-3.5-turbo",
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


# 10. Conversation API Routes

@app.post("/api/conversations")
async def create_conversation(body: ConversationCreate):
    """Create a new conversation."""
    logging.info(f"POST /api/conversations — title={body.title!r}")
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO conversations (title)
                    VALUES (%s)
                    RETURNING id, title, created_at
                    """,
                    (body.title,),
                )
                row = dict(cur.fetchone())
            conn.commit()
        row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
        logging.info(f"Conversation created with id={row['id']}")
        return JSONResponse(content=row, status_code=201)
    except Exception as e:
        logging.error(f"POST /api/conversations error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create conversation")


@app.get("/api/conversations")
async def get_conversations():
    """Return all conversations ordered by most recently updated."""
    logging.info("GET /api/conversations")
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM conversations
                    ORDER BY updated_at DESC
                    """
                )
                rows = cur.fetchall()
        conversations = [
            {
                **dict(row),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }
            for row in rows
        ]
        logging.info(f"Returning {len(conversations)} conversations")
        return JSONResponse(content=conversations)
    except Exception as e:
        logging.error(f"GET /api/conversations error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch conversations")


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: int):
    """Return a single conversation with all its messages."""
    logging.info(f"GET /api/conversations/{conversation_id}")
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, title, created_at, updated_at FROM conversations WHERE id = %s",
                    (conversation_id,),
                )
                conv_row = cur.fetchone()
                if conv_row is None:
                    raise HTTPException(status_code=404, detail="Conversation not found")

                cur.execute(
                    """
                    SELECT role, content, created_at
                    FROM conversation_messages
                    WHERE conversation_id = %s
                    ORDER BY created_at ASC
                    """,
                    (conversation_id,),
                )
                msg_rows = cur.fetchall()

        messages = [
            {
                **dict(m),
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            }
            for m in msg_rows
        ]
        result = {
            "id": conv_row["id"],
            "title": conv_row["title"],
            "created_at": conv_row["created_at"].isoformat() if conv_row["created_at"] else None,
            "updated_at": conv_row["updated_at"].isoformat() if conv_row["updated_at"] else None,
            "messages": messages,
        }
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"GET /api/conversations/{conversation_id} error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch conversation")

@app.post("/api/conversations/{conversation_id}/messages")
async def add_message(conversation_id: int, body: MessageCreate):
    """Add a user message to a conversation and return the Gemini reply."""
    logging.info(f"POST /api/conversations/{conversation_id}/messages")
    
    # 1. Check for Gemini Key (Updated variable name check)
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(status_code=503, detail="Gemini API key is not configured")

    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Verify conversation exists
                cur.execute("SELECT id FROM conversations WHERE id = %s", (conversation_id,))
                if cur.fetchone() is None:
                    raise HTTPException(status_code=404, detail="Conversation not found")

                # Fetch existing messages for context
                cur.execute(
                    "SELECT role, content FROM conversation_messages WHERE conversation_id = %s ORDER BY created_at ASC",
                    (conversation_id,),
                )
                history = [dict(r) for r in cur.fetchall()]

                # Persist the new user message
                cur.execute(
                    """
                    INSERT INTO conversation_messages (conversation_id, role, content)
                    VALUES (%s, 'user', %s)
                    RETURNING id, role, content, created_at
                    """,
                    (conversation_id, body.content),
                )
                user_msg = dict(cur.fetchone())
            conn.commit()

        # 2. Format History for Gemini (IMPORTANT: 'assistant' -> 'model')
        gemini_history = []
        for msg in history:
            # Gemini strictly requires 'model' instead of 'assistant'
            role = "model" if msg["role"] in ["assistant", "model"] else "user"
            gemini_history.append({"role": role, "parts": [{"text": msg["content"]}]})
        
        # 3. Call Gemini
        try:
            chat = client.chats.create(
                model="gemini-2.5-flash-lite", # Use 2.0 Flash for 2026 standards
                config=types.GenerateContentConfig(
                    system_instruction="You are a helpful assistant.",
                    temperature=0.7,
                ),
                history=gemini_history
            )
            response = chat.send_message(body.content)
            assistant_content = response.text
        except Exception as ai_err:
            logging.error(f"Gemini API Error: {ai_err}")
            raise HTTPException(status_code=502, detail=f"AI Provider Error: {str(ai_err)}")

        # 4. Persist the assistant reply
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO conversation_messages (conversation_id, role, content)
                    VALUES (%s, 'assistant', %s)
                    RETURNING id, role, content, created_at
                    """,
                    (conversation_id, assistant_content),
                )
                assistant_msg = dict(cur.fetchone())

                # Update conversation timestamp
                cur.execute(
                    "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (conversation_id,),
                )
            conn.commit()

        # 5. Format dates for JSON
        user_msg["created_at"] = user_msg["created_at"].isoformat() if user_msg.get("created_at") else None
        assistant_msg["created_at"] = assistant_msg["created_at"].isoformat() if assistant_msg.get("created_at") else None

        return JSONResponse(
            content={"user_message": user_msg, "assistant_message": assistant_msg},
            status_code=201,
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"POST /api/conversations/{conversation_id}/messages error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.post("/api/conversations/{conversation_id}/messagess")
async def add_message(conversation_id: int, body: MessageCreate):
    """Add a user message to a conversation and return the AI assistant reply."""
    logging.info(f"POST /api/conversations/{conversation_id}/messages")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured")
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Verify the conversation exists
                cur.execute("SELECT id FROM conversations WHERE id = %s", (conversation_id,))
                if cur.fetchone() is None:
                    raise HTTPException(status_code=404, detail="Conversation not found")

                # Fetch existing messages to build the full context for the model
                cur.execute(
                    """
                    SELECT role, content
                    FROM conversation_messages
                    WHERE conversation_id = %s
                    ORDER BY created_at ASC
                    """,
                    (conversation_id,),
                )
                history = [dict(r) for r in cur.fetchall()]

                # Persist the new user message
                cur.execute(
                    """
                    INSERT INTO conversation_messages (conversation_id, role, content)
                    VALUES (%s, 'user', %s)
                    RETURNING id, role, content, created_at
                    """,
                    (conversation_id, body.content),
                )
                user_msg = dict(cur.fetchone())

            conn.commit()

        # Build the message list for OpenAI (history + new user turn)
        openai_messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            *history,
            {"role": "user", "content": body.content},
        ]
        ############################
        gemini_history = []
        for msg in history:
            role = "model" if msg["role"] == "assistant" else "user"
            gemini_history.append({"role": role, "parts": [{"text": msg["content"]}]})
        
        try:
            # 3. Start a chat session with the history
            chat = client.chats.create(
                #model="gemini-2.0-flash",
                model="gemini-2.5-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction="You are a helpful assistant.",
                    temperature=0.7,
                ),
                history=gemini_history # This injects your previous messages
            )
        
            # 4. Send the new user message
            response = chat.send_message(body.content)
            
            # 5. Get the text (Equivalent to response.choices[0].message.content)
            assistant_content = response.text
        
        except Exception as e:
            print(f"Gemini Error: {e}")

        #####################

        # Persist the assistant reply and bump updated_at on the conversation
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO conversation_messages (conversation_id, role, content)
                    VALUES (%s, 'assistant', %s)
                    RETURNING id, role, content, created_at
                    """,
                    (conversation_id, assistant_content),
                )
                assistant_msg = dict(cur.fetchone())

                cur.execute(
                    "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (conversation_id,),
                )
            conn.commit()

        user_msg["created_at"] = user_msg["created_at"].isoformat() if user_msg["created_at"] else None
        assistant_msg["created_at"] = assistant_msg["created_at"].isoformat() if assistant_msg["created_at"] else None

        return JSONResponse(
            content={"user_message": user_msg, "assistant_message": assistant_msg},
            status_code=201,
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"POST /api/conversations/{conversation_id}/messages error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process message")


@app.patch("/api/conversations/{conversation_id}")
async def update_conversation(conversation_id: int, body: ConversationUpdate):
    """Update the title of an existing conversation."""
    logging.info(f"PATCH /api/conversations/{conversation_id} — title={body.title!r}")
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE conversations
                    SET title = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, title
                    """,
                    (body.title, conversation_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Conversation not found")
            conn.commit()
        logging.info(f"Conversation {conversation_id} title updated")
        return JSONResponse(content=dict(row))
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"PATCH /api/conversations/{conversation_id} error: {e}")
        raise HTTPException(status_code=500, detail="Failed to update conversation")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

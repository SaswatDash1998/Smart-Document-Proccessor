import os
import logging
import asyncpg
import httpx
import pandas as pd
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel
from cerebras.cloud.sdk import AsyncCerebras
from google import genai
from google.genai import types
import math

# ---------- GraphRAG v3 correct imports ----------
from graphrag.api.query import local_search, global_search, drift_search
from graphrag.config.load_config import load_config

logger = logging.getLogger(__name__)

# ================================================================
#  Globals
# ================================================================

db_pool: asyncpg.Pool | None = None

cerebras_client = AsyncCerebras(api_key="csk-pw2hvcj3yfy6myvjxj5fc9e4k3fdpwh4pn429thnmtcnewxe")
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

# Resolve absolute path so it works regardless of where uvicorn is launched from
_THIS_FILE    = Path(__file__).resolve()       # .../app/rag_agent.py
_PROJECT_ROOT = _THIS_FILE.parent.parent       # .../n8n_project/

GRAPHRAG_ROOT      = Path(os.getenv("GRAPHRAG_ROOT", str(_PROJECT_ROOT / "data"))).resolve()
GRAPHRAG_DATA_DIR  = GRAPHRAG_ROOT / "output"
GRAPHRAG_COMMUNITY = int(os.getenv("GRAPHRAG_COMMUNITY_LEVEL", "2"))


def get_db_pool() -> asyncpg.Pool:
    """Call this at request-time — never import db_pool directly (it's None at import time)."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="DB pool not ready yet.")
    return db_pool


# ================================================================
#  Lifespan
# ================================================================

@asynccontextmanager
async def rag_lifespan(app: FastAPI):
    global db_pool
    """db_pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "54320")),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
        database=os.getenv("DB_NAME", "postgres"),
        command_timeout=60,
        min_size=2,
        max_size=10,
    )"""

    db_pool = await asyncpg.create_pool(
        host=os.getenv('DB_HOST'),
        port=int(os.getenv('DB_PORT', '5432')),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        ssl='require',   # required for Azure PostgreSQL
        command_timeout=60,
    )

    async with db_pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.set_type_codec(
            "vector",
            encoder=lambda v: v,
            decoder=lambda v: v,
            schema="public",
            format="text",
        )
        
        # Create the documents table for vector RAG
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                embedding vector(768),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Add new columns if they don't exist (migration for existing tables)
        await conn.execute("""
            ALTER TABLE documents 
            ADD COLUMN IF NOT EXISTS filename TEXT
        """)
        
        await conn.execute("""
            ALTER TABLE documents 
            ADD COLUMN IF NOT EXISTS chunk_index INTEGER
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS documents_embedding_idx
            ON documents USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS documents_filename_chunk_idx
            ON documents (filename, chunk_index)
        """)
        
    logger.info("✅ DB pool ready with documents table")
    yield
    await db_pool.close()
    logger.info("✅ DB pool closed")


# ================================================================
#  Router + models
# ================================================================

router = APIRouter(prefix="/rag", tags=["RAG"])


class QueryRequest(BaseModel):
    question: str
    method: Literal["vector", "local", "global", "drift"] = "vector"


# ================================================================
#  Gemini embeddings  (Cerebras has NO embedding API)
# ================================================================

def clean_text(text: str) -> str:
    return " ".join(text.strip().split())


def normalize_embedding(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


async def get_embedding(text: str) -> list[float]:
    """Get embeddings from Gemini (same as main.py uses)."""
    from fastapi.concurrency import run_in_threadpool
    
    def get_gemini_embedding_sync(text: str) -> list[float]:
        text = clean_text(text)
        response = gemini_client.models.embed_content(
            model="gemini-embedding-001",
            contents=text[:8000],  # Limit text length
            config=types.EmbedContentConfig(
                output_dimensionality=768,
                task_type="RETRIEVAL_QUERY",  # Using QUERY for search
            ),
        )
        embedding = response.embeddings[0].values
        return normalize_embedding(embedding)
    
    # Run synchronous Gemini call in threadpool
    return await run_in_threadpool(get_gemini_embedding_sync, text)


# ================================================================
#  Vector RAG  (Ollama embed → pgvector → Cerebras generate)
# ================================================================

async def retrieve_relevant_chunks(question: str, top_k: int = 5) -> tuple[str, int]:
    pool = get_db_pool()
    embedding = await get_embedding(question)
    vector_str = "[" + ",".join(str(x) for x in embedding) + "]"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT content,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM   documents
            ORDER  BY embedding <=> $1::vector
            LIMIT  $2
            """,
            vector_str, top_k,
        )

    if not rows:
        return "No documents in the database yet. Upload documents first.", 0

    for i, row in enumerate(rows):
        logger.info(f"chunk {i+1}: similarity={row['similarity']:.3f}")

    return "\n\n---\n\n".join(r["content"] for r in rows), len(rows)


# ================================================================
#  GraphRAG v3  (load parquets → call API)
#
#  v3 changed completely from older versions:
#  - No more config_filepath / data_dir / root_dir string args
#  - You load GraphRagConfig yourself via load_config(root_dir)
#  - You load each required parquet DataFrame yourself
#  - Then pass everything explicitly to local_search / global_search / drift_search
# ================================================================

def _load_parquet(filename: str) -> pd.DataFrame:
    """Load a parquet file from the GraphRAG output directory."""
    path = GRAPHRAG_DATA_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"GraphRAG output file not found: {path}. "
            "Run POST /graphrag/index first."
        )
    return pd.read_parquet(path)


async def retrieve_graphrag(question: str, method: str) -> str:
    """
    GraphRAG v3 API — must load config + parquet DataFrames manually.

    To use Cerebras as the LLM, add to data/settings.yaml:
        llm:
          api_type: openai_chat
          api_base: https://api.cerebras.net/v1
          api_key: ${CEREBRAS_API_KEY}
          model: llama3.1-8b
    """
    try:
        config = load_config(root_dir=GRAPHRAG_ROOT)

        # DataFrames every method needs
        entities          = _load_parquet("entities.parquet")
        communities       = _load_parquet("communities.parquet")
        community_reports = _load_parquet("community_reports.parquet")

        if method == "global":
            response, _ = await global_search(
                config=config,
                entities=entities,
                communities=communities,
                community_reports=community_reports,
                community_level=GRAPHRAG_COMMUNITY,
                dynamic_community_selection=False,
                response_type="Multiple Paragraphs",
                query=question,
            )

        elif method == "drift":
            text_units    = _load_parquet("text_units.parquet")
            relationships = _load_parquet("relationships.parquet")
            response, _ = await drift_search(
                config=config,
                entities=entities,
                communities=communities,
                community_reports=community_reports,
                text_units=text_units,
                relationships=relationships,
                community_level=GRAPHRAG_COMMUNITY,
                response_type="Multiple Paragraphs",
                query=question,
            )

        else:  # local
            text_units    = _load_parquet("text_units.parquet")
            relationships = _load_parquet("relationships.parquet")
            # covariates is optional — pass None if not present
            try:
                covariates = _load_parquet("covariates.parquet")
            except FileNotFoundError:
                covariates = None

            response, _ = await local_search(
                config=config,
                entities=entities,
                communities=communities,
                community_reports=community_reports,
                text_units=text_units,
                relationships=relationships,
                covariates=covariates,
                community_level=GRAPHRAG_COMMUNITY,
                response_type="Multiple Paragraphs",
                query=question,
            )

        return response

    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("GraphRAG search failed")
        raise HTTPException(status_code=500, detail=f"GraphRAG error: {e}")


# ================================================================
#  /rag/ask
# ================================================================

@router.post("/ask")
async def ask_question(request: QueryRequest):
    # --- GraphRAG path ---
    if request.method in ("local", "global", "drift"):
        answer = await retrieve_graphrag(request.question, request.method)
        return {
            "question": request.question,
            "method":   request.method,
            "answer":   answer,
            "source":   "graphrag",
        }

    # --- Vector RAG path ---
    context, chunk_count = await retrieve_relevant_chunks(request.question)

    response = await cerebras_client.chat.completions.create(
        model="llama3.1-8b",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. Answer ONLY based on the provided context. "
                    "If the context doesn't contain the answer, say so clearly.\n\n"
                    f"Context:\n{context}"
                ),
            },
            {"role": "user", "content": request.question},
        ],
        max_tokens=1024,
    )

    return {
        "question":         request.question,
        "method":           "vector",
        "answer":           response.choices[0].message.content,
        "retrieved_chunks": chunk_count,
        "source":           "cerebras+pgvector+ollama",
    }


# ================================================================
#  /rag/debug  — sanity check
# ================================================================

@router.get("/debug")
async def debug_db():
    """Check document count in DB. If 0, inserts are failing — check Ollama."""
    pool = get_db_pool()
    async with pool.acquire() as conn:
        count  = await conn.fetchval("SELECT COUNT(*) FROM documents")
        sample = await conn.fetchrow(
            "SELECT id, length(content) AS chars, LEFT(content, 200) AS preview "
            "FROM documents LIMIT 1"
        )
    return {
        "document_count": count,
        "sample":         dict(sample) if sample else None,
        "ollama_model":   EMBEDDING_MODEL,
        "graphrag_index": {
            "output_dir_exists": GRAPHRAG_DATA_DIR.exists(),
            "parquet_files": [f.name for f in GRAPHRAG_DATA_DIR.glob("*.parquet")] if GRAPHRAG_DATA_DIR.exists() else [],
        },
    }
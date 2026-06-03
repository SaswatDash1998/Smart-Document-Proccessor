import logging
import subprocess
from pathlib import Path
from typing import List, Optional
import tempfile
import time

import asyncpg
import httpx
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from azure.storage.blob import BlobServiceClient
from google import genai
from google.genai import types
import math

import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# Import new modules
from app.auth import (
    create_api_key,
    list_api_keys,
    revoke_api_key,
    verify_api_key,
)
from app.metrics import (
    metrics_collector,
    collect_all_metrics,
    get_processing_history_stats,
)

# Remove in-memory jobs dict - now using database!
executor = ThreadPoolExecutor(max_workers=2)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import document processor (with fallback)
try:
    from app.processors.document_processor import process_document
    PROCESSOR_AVAILABLE = True
except ImportError:
    logger.error("Could not import process_document, using dummy fallback")
    PROCESSOR_AVAILABLE = False
    def process_document(file_path: str) -> str:
        return f"[Fallback] Could not process {file_path}."

# Import RAG router — db_pool lives there
from app.rag_agent import router as rag_router, rag_lifespan, get_db_pool

# ---------- Azure Blob Storage ----------
blob_service = BlobServiceClient.from_connection_string(
    os.getenv('AZURE_STORAGE_CONNECTION_STRING')
)

# ---------- Gemini AI Client ----------
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

# ---------- Directories ----------
RAW_DIR       = Path("data/raw")
PROCESSED_DIR = Path("data/processed")
INPUT_DIR     = Path("data/input")       # GraphRAG reads .txt files from here
GRAPHRAG_ROOT = Path("data")             # --root for graphrag CLI (fallback)

for folder in [RAW_DIR, PROCESSED_DIR, INPUT_DIR, GRAPHRAG_ROOT]:
    folder.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}


# ---------- Pydantic models ----------
class GraphRAGQuery(BaseModel):
    question: str
    method: str = "local"   # "local" | "global" | "drift"


def clean_text(text: str) -> str:
    return " ".join(text.strip().split())


def chunk_text_by_words(
    text: str,
    chunk_size: int = 500,
    overlap: int = 80,
) -> list[str]:
    """
    Simple word-based chunking.
    Good default for embeddings/RAG.
    """
    words = text.split()

    if len(words) <= chunk_size:
        return [text]

    chunks = []
    start = 0

    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)

        start += chunk_size - overlap

    return chunks


def normalize_embedding(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def get_gemini_embedding(text: str, title: str | None = None) -> list[float]:
    text = clean_text(text)

    response = gemini_client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
        config=types.EmbedContentConfig(
            output_dimensionality=768,
            task_type="RETRIEVAL_DOCUMENT",
            title=title,
        ),
    )

    embedding = response.embeddings[0].values
    return normalize_embedding(embedding)


# ---------- Lifespan ----------
# We reuse rag_lifespan so there's exactly ONE db pool, owned by rag_agent.
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with rag_lifespan(app):
        print("✅ Main app + RAG lifespan started")
        yield
    print("✅ Main app + RAG lifespan stopped")


# ---------- App ----------
app = FastAPI(
    title="GraphRAG Document Intelligence",
    description="Upload, extract, index with GraphRAG, and query your documents",
    lifespan=lifespan,
)
app.include_router(rag_router)


# ---------- Helpers ----------
def is_index_built() -> bool:
    """True if GraphRAG has produced at least one .parquet output file."""
    output_dir = GRAPHRAG_ROOT / "output"
    return output_dir.exists() and any(output_dir.glob("*.parquet"))


# ================================================================
#  Core endpoints
# ================================================================

@app.get("/")
def health_check():
    return {
        "status": "running",
        "processor_available": PROCESSOR_AVAILABLE,
        "index_exists": is_index_built(),
    }


@app.post("/process-documents")
async def process_documents(files: List[UploadFile] = File(...), request: Request = None):
    """
    Upload documents → extract text → chunk → embed with Gemini → store in pgvector
    AND upload raw/extracted docs to Azure Blob Storage for GraphRAG.
    """
    pool = get_db_pool()
    results = []

    for file in files:
        start_time = time.time()
        history_id = None
        safe_filename = Path(file.filename).name
        ext = Path(safe_filename).suffix.lower()
        stem = Path(safe_filename).stem

        if ext not in ALLOWED_EXTENSIONS:
            results.append({
                "filename": safe_filename,
                "status": "error",
                "message": "Unsupported type",
            })
            continue

        try:
            content = await file.read()
            file_size = len(content)
            
            # Create processing history record
            async with pool.acquire() as conn:
                history_id = await conn.fetchval(
                    """
                    INSERT INTO processing_history (
                        filename, file_size, file_type, status, user_id
                    ) VALUES ($1, $2, $3, 'in_progress', $4)
                    RETURNING id
                    """,
                    safe_filename,
                    file_size,
                    ext,
                    getattr(request.state, 'api_key_id', 'default') if request else 'default',
                )

            # 1. Upload raw file to Azure Blob Storage
            try:
                def upload_raw_blob():
                    blob_client = blob_service.get_blob_client(
                        container="raw-documents",
                        blob=safe_filename,
                    )
                    blob_client.upload_blob(content, overwrite=True)

                await run_in_threadpool(upload_raw_blob)
                logger.info(f"✅ Uploaded {safe_filename} to raw-documents blob")

            except Exception as e:
                logger.error(f"❌ Failed to upload {safe_filename} to blob: {e}")
                results.append({
                    "filename": safe_filename,
                    "status": "error",
                    "message": f"Blob upload error: {str(e)}",
                })
                continue

            # 2. Save temporarily for process_document()
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)

            try:
                extracted_text = await run_in_threadpool(
                    process_document,
                    str(tmp_path),
                )
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"Could not delete temporary file {tmp_path}: {e}")

            extracted_text = clean_text(extracted_text)

            if not extracted_text:
                results.append({
                    "filename": safe_filename,
                    "status": "error",
                    "message": "No text extracted from document",
                })
                continue

            # 3. Upload extracted text for GraphRAG
            try:
                def upload_graphrag_input():
                    blob_client = blob_service.get_blob_client(
                        container="graphrag-input",
                        blob=f"{stem}.txt",
                    )
                    blob_client.upload_blob(
                        extracted_text.encode("utf-8"),
                        overwrite=True,
                    )

                await run_in_threadpool(upload_graphrag_input)
                logger.info(f"✅ Uploaded {stem}.txt to graphrag-input blob")

            except Exception as e:
                logger.error(f"❌ Failed to upload {stem}.txt to blob: {e}")
                results.append({
                    "filename": safe_filename,
                    "status": "error",
                    "message": f"GraphRAG input upload error: {str(e)}",
                })
                continue

            # 4. Chunk text
            chunks = chunk_text_by_words(
                extracted_text,
                chunk_size=500,
                overlap=80,
            )

            inserted_chunks = 0

            # 5. Embed each chunk and insert into pgvector
            try:
                async with pool.acquire() as conn:
                    for chunk_index, chunk in enumerate(chunks):
                        embedding = await run_in_threadpool(
                            get_gemini_embedding,
                            chunk,
                            stem,
                        )

                        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

                        await conn.execute(
                            """
                            INSERT INTO documents (
                                filename,
                                chunk_index,
                                content,
                                embedding
                            )
                            VALUES ($1, $2, $3, $4::vector)
                            """,
                            safe_filename,
                            chunk_index,
                            chunk,
                            embedding_str,
                        )

                        inserted_chunks += 1

                logger.info(
                    f"✅ Inserted {inserted_chunks} chunks for {safe_filename}"
                )

            except Exception as e:
                logger.error(f"❌ Failed to embed/insert {safe_filename}: {e}")
                results.append({
                    "filename": safe_filename,
                    "status": "error",
                    "message": f"Embedding/DB error: {str(e)}",
                })
                continue

            # Update processing history with success
            processing_time_ms = int((time.time() - start_time) * 1000)
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE processing_history
                    SET status = 'success',
                        characters_extracted = $1,
                        chunks_created = $2,
                        chunks_inserted = $3,
                        processing_time_ms = $4,
                        graphrag_input_path = $5,
                        raw_blob_path = $6,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = $7
                    """,
                    len(extracted_text),
                    len(chunks),
                    inserted_chunks,
                    processing_time_ms,
                    f"graphrag-input/{stem}.txt",
                    f"raw-documents/{safe_filename}",
                    history_id,
                )
            
            # Track metrics
            metrics_collector.increment_counter("documents_processed", tags={"status": "success"})
            metrics_collector.record_histogram("document_processing_time_ms", processing_time_ms)
            
            results.append({
                "filename": safe_filename,
                "status": "success",
                "characters_extracted": len(extracted_text),
                "words_extracted": len(extracted_text.split()),
                "chunks_created": len(chunks),
                "chunks_inserted": inserted_chunks,
                "processing_time_ms": processing_time_ms,
                "preview": extracted_text[:300],
                "graphrag_input_ready": f"Azure Blob: graphrag-input/{stem}.txt",
                "raw_blob": f"Azure Blob: raw-documents/{safe_filename}",
                "history_id": history_id,
            })

        except Exception as e:
            logger.error(f"❌ Unexpected error processing {safe_filename}: {e}")
            
            # Update processing history with failure
            if history_id:
                processing_time_ms = int((time.time() - start_time) * 1000)
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE processing_history
                        SET status = 'failed',
                            error_message = $1,
                            processing_time_ms = $2,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = $3
                        """,
                        str(e),
                        processing_time_ms,
                        history_id,
                    )
            
            # Track metrics
            metrics_collector.increment_counter("documents_processed", tags={"status": "failed"})
            
            results.append({
                "filename": safe_filename,
                "status": "error",
                "message": f"Unexpected error: {str(e)}",
                "history_id": history_id,
            })

    return {
        "documents_received": len(results),
        "documents_processed": sum(1 for r in results if r["status"] == "success"),
        "results": results,
    }


# ================================================================
#  GraphRAG index management  (CLI fallback — useful for debugging)
# ================================================================

@app.post("/graphrag/index")
async def build_graphrag_index():
    """
    Build / rebuild the GraphRAG knowledge graph.
    Downloads files from Azure Blob Storage, then indexes them locally.
    Returns immediately with a job_id — poll /graphrag/index/status/{job_id} for progress.
    """
    # STEP 1: Download files from Azure Blob Storage to local data/input/
    logger.info("📥 Downloading files from Azure Blob Storage...")
    
    try:
        container_client = blob_service.get_container_client("graphrag-input")
        blobs = list(container_client.list_blobs())
        
        txt_blobs = [blob for blob in blobs if blob.name.endswith('.txt')]
        
        if not txt_blobs:
            raise HTTPException(
                status_code=400,
                detail="No .txt files found in Azure Blob 'graphrag-input' container. Upload documents first via POST /process-documents.",
            )
        
        # Download each blob to local data/input/
        downloaded_count = 0
        for blob in txt_blobs:
            try:
                blob_client = container_client.get_blob_client(blob.name)
                file_path = INPUT_DIR / blob.name
                
                # Download blob content
                def download_blob():
                    blob_data = blob_client.download_blob().readall()
                    file_path.write_bytes(blob_data)
                
                await run_in_threadpool(download_blob)
                downloaded_count += 1
                logger.info(f"✅ Downloaded {blob.name} from Azure Blob")
            except Exception as e:
                logger.error(f"❌ Failed to download {blob.name}: {e}")
                # Continue with other files
        
        if downloaded_count == 0:
            raise HTTPException(
                status_code=500,
                detail="Failed to download any files from Azure Blob Storage. Check container access.",
            )
        
        logger.info(f"✅ Downloaded {downloaded_count} file(s) from Azure Blob to data/input/")
        
    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except Exception as e:
        logger.error(f"❌ Failed to access Azure Blob Storage: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to prepare input files from Azure Blob: {str(e)}",
        )
    
    # STEP 2: Verify files are now available locally
    if not any(INPUT_DIR.glob("*.txt")):
        raise HTTPException(
            status_code=500,
            detail="Files downloaded from Azure Blob but not found in data/input/. Check file permissions.",
        )

    pool = get_db_pool()
    
    # AUTO-RECOVERY: Reset stuck jobs (running >30 minutes) before checking
    async with pool.acquire() as conn:
        stuck_count = await conn.fetchval(
            """
            UPDATE graphrag_jobs
            SET status = 'failed',
                error_message = 'Auto-cancelled: exceeded 30-minute timeout',
                completed_at = CURRENT_TIMESTAMP
            WHERE status = 'running'
            AND started_at < NOW() - INTERVAL '30 minutes'
            RETURNING 1
            """
        )
        if stuck_count:
            logger.warning(f"⚠️ Auto-recovered {stuck_count} stuck job(s)")
    
    # Prevent duplicate runs - check database instead of memory
    async with pool.acquire() as conn:
        running_job = await conn.fetchrow(
            "SELECT job_id, started_at FROM graphrag_jobs WHERE status = 'running' LIMIT 1"
        )
        
        if running_job:
            return {
                "job_id": str(running_job['job_id']),
                "status": "already_running",
                "message": "Indexing already in progress",
                "started_at": running_job['started_at'].isoformat() if running_job['started_at'] else None
            }

    # Create new job in database
    job_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO graphrag_jobs (job_id, status, started_at)
            VALUES ($1, 'running', CURRENT_TIMESTAMP)
            """,
            job_id,
        )

    def run_index():
        """Run in thread pool - updates database when complete"""
        import asyncio
        
        async def update_job_status():
            pool = get_db_pool()
            timeout_seconds = int(os.getenv("GRAPHRAG_INDEX_TIMEOUT", "1800"))  # 30 min default
            
            try:
                cmd = ["graphrag", "index", "--root", str(GRAPHRAG_ROOT)]
                logger.info(f"Running: {' '.join(cmd)} (timeout: {timeout_seconds}s)")
                
                # Add timeout to prevent infinite hangs
                result = subprocess.run(
                    cmd, 
                    capture_output=True, 
                    text=True, 
                    shell=False,
                    timeout=timeout_seconds
                )
                success = result.returncode == 0
                
                # Update database with results
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE graphrag_jobs
                        SET status = $1,
                            return_code = $2,
                            stdout = $3,
                            stderr = $4,
                            index_exists = $5,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE job_id = $6
                        """,
                        "completed" if success else "failed",
                        result.returncode,
                        result.stdout[-3000:] if result.stdout else "",
                        result.stderr[-3000:] if result.stderr else "",
                        success and is_index_built(),
                        job_id,
                    )
                logger.info(f"✅ Job {job_id} {'completed' if success else 'failed'}")
                
            except subprocess.TimeoutExpired as e:
                logger.error(f"❌ Job {job_id} timed out after {timeout_seconds}s")
                # Update database with timeout error
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE graphrag_jobs
                        SET status = 'failed',
                            error_message = $1,
                            stderr = $2,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE job_id = $3
                        """,
                        f"Indexing timed out after {timeout_seconds} seconds",
                        (e.stderr.decode() if e.stderr else "")[-3000:],
                        job_id,
                    )
            except Exception as e:
                logger.error(f"❌ Job {job_id} failed with exception: {e}")
                # Update database with error
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE graphrag_jobs
                        SET status = 'failed',
                            error_message = $1,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE job_id = $2
                        """,
                        str(e),
                        job_id,
                    )
        
        # Run async function in new event loop (thread pool context)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(update_job_status())
        finally:
            loop.close()

    # Submit to thread pool
    loop = asyncio.get_event_loop()
    loop.run_in_executor(executor, run_index)

    return {"job_id": str(job_id), "status": "running"}  # ✅ Returns immediately


# ADD this new endpoint after the one above:
@app.get("/graphrag/index/status/{job_id}")
async def get_index_status(job_id: str):
    """Poll this after POST /graphrag/index to check if indexing is done."""
    pool = get_db_pool()
    
    # Auto-cleanup old jobs before checking
    async with pool.acquire() as conn:
        await conn.execute("SELECT cleanup_old_graphrag_jobs()")
    
    # Fetch job from database
    async with pool.acquire() as conn:
        job_record = await conn.fetchrow(
            "SELECT * FROM graphrag_jobs WHERE job_id = $1",
            uuid.UUID(job_id),
        )
    
    if not job_record:
        raise HTTPException(
            status_code=404,
            detail="Job not found. It may have been cleaned up (jobs are deleted after 24 hours) or never existed. Please start a new indexing job."
        )
    
    # Convert record to dict and format for response
    return {
        "job_id": str(job_record['job_id']),
        "status": job_record['status'],
        "created_at": job_record['created_at'].isoformat() if job_record['created_at'] else None,
        "started_at": job_record['started_at'].isoformat() if job_record['started_at'] else None,
        "completed_at": job_record['completed_at'].isoformat() if job_record['completed_at'] else None,
        "return_code": job_record['return_code'],
        "stdout": job_record['stdout'],
        "stderr": job_record['stderr'],
        "error": job_record['error_message'],
        "index_exists": job_record['index_exists'],
    }


# NEW: Cancel/Reset endpoints for stuck jobs
@app.delete("/graphrag/index/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a specific GraphRAG job by marking it as failed."""
    pool = get_db_pool()
    
    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            """
            UPDATE graphrag_jobs
            SET status = 'failed',
                error_message = 'Manually cancelled by user',
                completed_at = CURRENT_TIMESTAMP
            WHERE job_id = $1 AND status = 'running'
            RETURNING job_id, status
            """,
            uuid.UUID(job_id),
        )
    
    if not result:
        raise HTTPException(
            status_code=404,
            detail="Job not found or already completed/failed"
        )
    
    logger.info(f"✅ Job {job_id} cancelled by user")
    return {
        "message": "Job cancelled successfully",
        "job_id": str(result['job_id']),
        "status": result['status']
    }


@app.post("/graphrag/reset-stuck-jobs")
async def reset_stuck_jobs(timeout_minutes: int = 30):
    """
    Reset all jobs that have been running longer than specified timeout.
    Default: 30 minutes. Returns count of jobs reset.
    """
    pool = get_db_pool()
    
    async with pool.acquire() as conn:
        # Get count before update - FIX: Use proper interval syntax
        stuck_jobs = await conn.fetch(
            """
            SELECT job_id, started_at, NOW() - started_at as runtime
            FROM graphrag_jobs
            WHERE status = 'running'
            AND started_at < NOW() - INTERVAL '1 minute' * $1
            """,
            timeout_minutes,
        )
        
        if not stuck_jobs:
            return {
                "message": "No stuck jobs found",
                "reset_count": 0,
                "timeout_minutes": timeout_minutes
            }
        
        # Update stuck jobs - FIX: Use proper interval syntax
        await conn.execute(
            """
            UPDATE graphrag_jobs
            SET status = 'failed',
                error_message = $1,
                completed_at = CURRENT_TIMESTAMP
            WHERE status = 'running'
            AND started_at < NOW() - INTERVAL '1 minute' * $2
            """,
            f"Reset by user: exceeded {timeout_minutes}-minute timeout",
            timeout_minutes,
        )
    
    logger.warning(f"⚠️ Reset {len(stuck_jobs)} stuck job(s) by user request")
    
    return {
        "message": f"Successfully reset {len(stuck_jobs)} stuck job(s)",
        "reset_count": len(stuck_jobs),
        "timeout_minutes": timeout_minutes,
        "jobs_reset": [
            {
                "job_id": str(job['job_id']),
                "runtime_seconds": int(job['runtime'].total_seconds())
            }
            for job in stuck_jobs
        ]
    }


@app.post("/graphrag/reset-lancedb")
async def reset_lancedb():
    """
    Delete LanceDB to fix dimension mismatch issues.
    Forces rebuild with correct vector dimensions on next index.
    """
    import shutil
    
    lancedb_path = GRAPHRAG_ROOT / "output" / "lancedb"
    
    if lancedb_path.exists():
        try:
            shutil.rmtree(lancedb_path)
            logger.info(f"✅ Deleted LanceDB at {lancedb_path}")
            return {
                "status": "success",
                "message": "LanceDB deleted successfully. Run 'Build Index' to rebuild with correct dimensions.",
                "path": str(lancedb_path)
            }
        except Exception as e:
            logger.error(f"❌ Failed to delete LanceDB: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete LanceDB: {str(e)}"
            )
    else:
        return {
            "status": "not_found",
            "message": "LanceDB directory doesn't exist (this is normal if index hasn't been built yet).",
            "path": str(lancedb_path)
        }


# KEEP your existing graphrag_status unchanged:
@app.get("/graphrag/status")
async def graphrag_status():
    return {
        "index_exists": is_index_built(),
        "root_directory": str(GRAPHRAG_ROOT),
        "input_files_count": len(list(INPUT_DIR.glob("*.txt"))),
        "output_parquet_count": len(list((GRAPHRAG_ROOT / "output").glob("*.parquet"))) if (GRAPHRAG_ROOT / "output").exists() else 0,
    }


@app.post("/graphrag/visualize")
async def visualize_graph():
    """
    Generate an interactive Pyvis visualization of the knowledge graph.
    Returns path to the generated HTML file.
    """
    output_dir = GRAPHRAG_ROOT / "output"
    
    if not output_dir.exists():
        raise HTTPException(
            status_code=400,
            detail="No GraphRAG output directory found. Build the index first."
        )
    
    required_files = ["entities.parquet", "relationships.parquet"]
    missing_files = [f for f in required_files if not (output_dir / f).exists()]
    
    if missing_files:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required files: {', '.join(missing_files)}. Build the index first."
        )
    
    try:
        from app.visualize_graph import visualize_graphrag
        
        # Generate visualization
        viz_path = await run_in_threadpool(
            visualize_graphrag,
            output_dir,
            "graph_visualization.html"
        )
        
        return {
            "status": "success",
            "message": "Visualization generated successfully",
            "file_path": str(viz_path),
            "relative_path": f"data/output/graph_visualization.html",
            "url": "/graphrag/visualization",
        }
        
    except Exception as e:
        logger.exception("Failed to generate visualization")
        raise HTTPException(
            status_code=500,
            detail=f"Visualization failed: {str(e)}"
        )


@app.get("/graphrag/visualization", response_class=HTMLResponse)
async def serve_visualization():
    """Serve the generated visualization HTML file."""
    viz_path = GRAPHRAG_ROOT / "output" / "graph_visualization.html"
    
    if not viz_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Visualization not found. Generate it first via POST /graphrag/visualize"
        )
    
    return viz_path.read_text(encoding="utf-8")


# ================================================================
#  Dashboard (HTML)
# ================================================================

# ================================================================
#  New Endpoints: Processing History, Stats, Auth, Metrics
# ================================================================

@app.get("/documents/history")
async def get_processing_history(
    limit: int = 50,
    status: Optional[str] = None,
    request: Request = None,
):
    """Get document processing history"""
    pool = get_db_pool()
    
    async with pool.acquire() as conn:
        query = "SELECT * FROM processing_history"
        params = []
        
        if status:
            query += " WHERE status = $1"
            params.append(status)
        
        query += " ORDER BY created_at DESC LIMIT " + str(min(limit, 1000))
        
        rows = await conn.fetch(query, *params)
        return {
            "total": len(rows),
            "history": [dict(row) for row in rows],
        }


@app.get("/documents/history/{history_id}")
async def get_processing_detail(history_id: int):
    """Get detailed information about a specific processing job"""
    pool = get_db_pool()
    
    async with pool.acquire() as conn:
        record = await conn.fetchrow(
            "SELECT * FROM processing_history WHERE id = $1",
            history_id,
        )
        
        if not record:
            raise HTTPException(status_code=404, detail="Processing record not found")
        
        return dict(record)


@app.get("/documents/stats")
async def get_document_stats(days: int = 7):
    """Get document processing statistics"""
    pool = get_db_pool()
    return await get_processing_history_stats(pool, days)


# Auth endpoints
@app.post("/auth/keys")
async def generate_api_key(
    key_name: str,
    rate_limit: int = 100,
    expires_in_days: Optional[int] = None,
):
    """Generate a new API key (admin only - add proper auth here)"""
    pool = get_db_pool()
    plaintext_key, key_id = await create_api_key(
        pool,
        key_name=key_name,
        rate_limit=rate_limit,
        expires_in_days=expires_in_days,
    )
    
    return {
        "key_id": key_id,
        "api_key": plaintext_key,
        "warning": "Save this key now! It won't be shown again.",
        "usage": f"Include in requests as header: X-API-Key: {plaintext_key}",
    }


@app.get("/auth/keys")
async def list_all_keys(include_inactive: bool = False):
    """List all API keys"""
    pool = get_db_pool()
    keys = await list_api_keys(pool, include_inactive)
    return {"keys": keys}


@app.delete("/auth/keys/{key_id}")
async def delete_api_key(key_id: int):
    """Revoke an API key"""
    pool = get_db_pool()
    success = await revoke_api_key(pool, key_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")
    
    return {"message": "API key revoked successfully"}


# Metrics endpoints
@app.get("/metrics")
async def get_metrics():
    """Get all system metrics (JSON format) - with error handling"""
    try:
        pool = get_db_pool()
        metrics = await collect_all_metrics(pool)
        return metrics
    except Exception as e:
        logger.error(f"❌ Failed to collect metrics: {e}")
        # Return minimal valid JSON instead of crashing
        return {
            "error": "Failed to collect metrics",
            "message": str(e),
            "timestamp": datetime.utcnow().isoformat(),
            "data_available": False
        }


@app.get("/metrics/prometheus", response_class=PlainTextResponse)
async def get_prometheus_metrics():
    """Get metrics in Prometheus text format"""
    pool = get_db_pool()
    # Collect fresh metrics
    await collect_all_metrics(pool)
    return metrics_collector.to_prometheus_format()


@app.get("/api/requests/recent")
async def get_recent_requests(limit: int = 100):
    """Get recent API requests for audit"""
    pool = get_db_pool()
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT 
                r.*,
                k.key_name
            FROM api_requests r
            LEFT JOIN api_keys k ON r.api_key_id = k.id
            ORDER BY r.created_at DESC
            LIMIT $1
            """,
            min(limit, 1000),
        )
        
        return {
            "total": len(rows),
            "requests": [dict(row) for row in rows],
        }


@app.get("/dashboard", response_class=HTMLResponse)
async def unified_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Document Intelligence</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #1e293b, #0f172a);
                min-height: 100vh;
                padding: 2rem;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
                background: #0f172a;
                border-radius: 1.5rem;
                box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
                overflow: hidden;
            }
            .header { background: #1e293b; padding: 1.5rem 2rem; border-bottom: 1px solid #334155; }
            .header h1 { color: #38bdf8; font-size: 1.8rem; }
            .header p { color: #94a3b8; margin-top: 0.5rem; }
            .tabs { display: flex; background: #1e293b; padding: 0 2rem; gap: 1rem; }
            .tab {
                padding: 0.75rem 1.5rem; background: none; border: none;
                color: #94a3b8; font-size: 1rem; cursor: pointer;
                transition: all 0.2s; border-radius: 0.5rem 0.5rem 0 0;
            }
            .tab.active { background: #0f172a; color: #38bdf8; border-bottom: 2px solid #38bdf8; }
            .tab-content { display: none; padding: 2rem; background: #0f172a; color: #e2e8f0; }
            .tab-content.active { display: block; }
            .upload-area {
                border: 2px dashed #334155; border-radius: 1rem; padding: 2rem;
                text-align: center; background: #1e293b; cursor: pointer; transition: all 0.2s;
            }
            .upload-area:hover, .upload-area.drag-over { border-color: #38bdf8; }
            .file-list { margin-top: 1rem; background: #1e293b; border-radius: 0.5rem; max-height: 200px; overflow-y: auto; }
            .file-item { padding: 0.5rem; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; }
            button {
                background: #3b82f6; color: white; border: none;
                padding: 0.6rem 1.2rem; border-radius: 0.5rem; cursor: pointer;
                font-weight: bold; transition: background 0.2s;
            }
            button:hover { background: #2563eb; }
            button:disabled { background: #475569; cursor: not-allowed; }
            button.secondary { background: #6366f1; }
            button.secondary:hover { background: #4f46e5; }
            .result-card { background: #1e293b; border-radius: 0.75rem; padding: 1rem; margin-top: 1rem; border-left: 4px solid; }
            .result-card.success { border-left-color: #10b981; }
            .result-card.error { border-left-color: #ef4444; }
            .preview { background: #0f172a; padding: 0.75rem; border-radius: 0.5rem; font-family: monospace; font-size: 0.85rem; white-space: pre-wrap; margin-top: 0.5rem; }
            .query-row { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; align-items: flex-end; }
            .query-input { flex: 2; padding: 0.75rem; background: #1e293b; border: 1px solid #334155; border-radius: 0.5rem; color: white; font-size: 1rem; }
            select { padding: 0.75rem; background: #1e293b; border: 1px solid #334155; border-radius: 0.5rem; color: white; font-size: 0.9rem; }
            .method-hint { color: #94a3b8; font-size: 0.8rem; margin-bottom: 0.75rem; }
            .answer-box { background: #1e293b; border-radius: 0.75rem; padding: 1rem; margin-top: 1rem; white-space: pre-wrap; }
            .answer-meta { color: #94a3b8; font-size: 0.8rem; margin-top: 0.5rem; }
            .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: white; animation: spin 1s linear infinite; margin-left: 0.5rem; vertical-align: middle; }
            @keyframes spin { to { transform: rotate(360deg); } }
            .badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 9999px; font-size: 0.7rem; font-weight: bold; margin-left: 0.5rem; }
            .badge.graph { background: #7c3aed20; color: #a78bfa; }
            .badge.vector { background: #0284c720; color: #38bdf8; }
        </style>
    </head>
    <body>
    <div class="container">
        <div class="header">
            <h1>🧠 Document Intelligence</h1>
            <p>Upload → Process → Query with Vector RAG (Cerebras) or GraphRAG</p>
        </div>
        <div class="tabs">
            <button class="tab active" data-tab="upload">📤 Upload & Process</button>
            <button class="tab" data-tab="query">💬 Query</button>
            <button class="tab" data-tab="graphrag">🕸️ GraphRAG Index</button>
            <button class="tab" data-tab="history">📋 Processing History</button>
            <button class="tab" data-tab="apikeys">🔑 API Keys</button>
            <button class="tab" data-tab="metrics">📊 Metrics</button>
        </div>

        <!-- Upload tab -->
        <div id="upload-tab" class="tab-content active">
            <div class="upload-area" id="dropZone">
                <div style="font-size:3rem">📁</div>
                <p>Drag & drop files (PDF, DOCX, TXT)</p>
                <input type="file" id="fileInput" multiple accept=".pdf,.docx,.txt" style="display:none">
                <button type="button" id="selectFilesBtn" style="margin-top:1rem">Select files</button>
                <div id="fileList" class="file-list" style="margin-top:1rem;display:none"></div>
            </div>
            <div style="margin-top:1rem;text-align:center">
                <button id="processBtn">🚀 Process Documents</button>
            </div>
            <div id="uploadResults" style="margin-top:2rem"></div>
        </div>

        <!-- Query tab -->
        <div id="query-tab" class="tab-content">
            <div class="method-hint">
                <strong>vector</strong> — fast, factual Q&A via Cerebras + pgvector &nbsp;|&nbsp;
                <strong>local</strong> — entity-level graph search &nbsp;|&nbsp;
                <strong>global</strong> — broad themes &nbsp;|&nbsp;
                <strong>drift</strong> — exploratory
            </div>
            <div class="query-row">
                <input type="text" id="questionInput" class="query-input" placeholder="Ask a question about your documents...">
                <select id="methodSelect">
                    <option value="vector">vector (Cerebras)</option>
                    <option value="local">local (GraphRAG)</option>
                    <option value="global">global (GraphRAG)</option>
                    <option value="drift">drift (GraphRAG)</option>
                </select>
                <button id="queryBtn">Ask</button>
            </div>
            <div id="queryAnswer" class="answer-box" style="display:none"></div>
            <div id="queryError" style="color:#f87171;margin-top:0.5rem"></div>
        </div>

        <!-- GraphRAG tab -->
        <div id="graphrag-tab" class="tab-content">
            <p style="color:#94a3b8;margin-bottom:1rem">
                Build the knowledge graph from all uploaded documents.
                Run this after uploading new files. It may take several minutes.
            </p>
            <div style="display:flex;gap:1rem;align-items:center;flex-wrap:wrap;margin-bottom:1rem">
                <button id="indexBtn" class="secondary">🕸️ Build GraphRAG Index</button>
                <button id="cancelJobBtn" style="background:#ef4444;display:none">🛑 Cancel Running Job</button>
                <button id="statusBtn">📊 Check Status</button>
                <button id="visualizeBtn" style="background:#10b981">🎨 Visualize Graph</button>
                <button id="resetStuckJobsBtn" style="background:#ef4444">🔄 Reset Stuck Jobs</button>
            </div>
            <div id="jobStatusDisplay" style="background:#1e293b;padding:1rem;border-radius:0.5rem;margin-bottom:1rem;display:none">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <div>
                        <strong id="jobStatusText">⏳ Job Status</strong>
                        <div id="jobElapsedTime" style="color:#94a3b8;font-size:0.85rem;margin-top:0.25rem"></div>
                    </div>
                    <div id="jobProgressIndicator" style="text-align:right;font-size:0.85rem;color:#94a3b8"></div>
                </div>
            </div>
            <div id="indexResult" class="answer-box" style="display:none;margin-top:1rem"></div>
        </div>

        <!-- Processing History tab -->
        <div id="history-tab" class="tab-content">
            <div style="display:flex;gap:1rem;margin-bottom:1rem;align-items:center">
                <select id="historyFilter" style="padding:0.75rem">
                    <option value="">All Status</option>
                    <option value="success">Success</option>
                    <option value="failed">Failed</option>
                    <option value="in_progress">In Progress</option>
                </select>
                <button id="refreshHistoryBtn">🔄 Refresh</button>
                <button id="getStatsBtn" class="secondary">📊 Get Statistics</button>
            </div>
            <div id="historyResults" style="max-height:600px;overflow-y:auto"></div>
        </div>

        <!-- API Keys tab -->
        <div id="apikeys-tab" class="tab-content">
            <div style="background:#1e293b;padding:1rem;border-radius:0.5rem;margin-bottom:1rem">
                <h3 style="color:#38bdf8;margin-bottom:1rem">Generate New API Key</h3>
                <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:0.5rem">
                    <input type="text" id="keyNameInput" placeholder="Key name (e.g., 'Production Key')" style="flex:2;padding:0.75rem;background:#0f172a;border:1px solid #334155;border-radius:0.5rem;color:white">
                    <input type="number" id="rateLimitInput" placeholder="Rate limit (req/hr)" value="100" style="flex:1;padding:0.75rem;background:#0f172a;border:1px solid #334155;border-radius:0.5rem;color:white">
                    <button id="generateKeyBtn">🔑 Generate Key</button>
                </div>
                <div id="newKeyDisplay" style="margin-top:1rem;display:none;background:#10b98120;padding:1rem;border-radius:0.5rem;border:1px solid #10b981">
                    <strong style="color:#10b981">⚠️ Save this key now! It won't be shown again.</strong>
                    <div style="margin-top:0.5rem;font-family:monospace;color:#e2e8f0;word-break:break-all" id="newKeyValue"></div>
                    <button onclick="navigator.clipboard.writeText(document.getElementById('newKeyValue').innerText)" style="margin-top:0.5rem;background:#10b981">📋 Copy to Clipboard</button>
                </div>
            </div>
            <h3 style="color:#38bdf8;margin-bottom:1rem">Existing Keys</h3>
            <div id="keysListResults"></div>
        </div>

        <!-- Metrics tab -->
        <div id="metrics-tab" class="tab-content">
            <div style="display:flex;gap:1rem;margin-bottom:1rem">
                <button id="refreshMetricsBtn">🔄 Refresh Metrics</button>
                <button id="downloadPrometheusBtn" class="secondary">⬇️ Download Prometheus Format</button>
            </div>
            <div id="metricsResults" style="max-height:600px;overflow-y:auto"></div>
        </div>
    </div>

    <script>
        // ---- Tab switching ----
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                const id = tab.dataset.tab;
                document.querySelectorAll('.tab,.tab-content').forEach(el => el.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById(id + '-tab').classList.add('active');
            });
        });

        // ---- File handling ----
        let selectedFiles = [];
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');

        ['dragenter','dragover','dragleave','drop'].forEach(ev => dropZone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); }));
        ['dragenter','dragover'].forEach(ev => dropZone.addEventListener(ev, () => dropZone.classList.add('drag-over')));
        ['dragleave','drop'].forEach(ev => dropZone.addEventListener(ev, () => dropZone.classList.remove('drag-over')));
        dropZone.addEventListener('drop', e => addFiles(e.dataTransfer.files));
        document.getElementById('selectFilesBtn').addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', e => addFiles(e.target.files));

        function addFiles(files) {
            for (let f of files) {
                const ext = f.name.split('.').pop().toLowerCase();
                if (['pdf','docx','txt'].includes(ext)) selectedFiles.push(f);
                else alert(`Skipped ${f.name}: unsupported type`);
            }
            renderFileList();
        }
        function renderFileList() {
            const div = document.getElementById('fileList');
            div.style.display = selectedFiles.length ? 'block' : 'none';
            div.innerHTML = selectedFiles.map(f => `<div class="file-item">📄 ${f.name} (${(f.size/1024).toFixed(1)} KB)</div>`).join('');
        }

        // ---- Process documents ----
        document.getElementById('processBtn').addEventListener('click', async () => {
            if (!selectedFiles.length) return alert('Please select files');
            const btn = document.getElementById('processBtn');
            btn.disabled = true; btn.innerHTML = 'Processing... <span class="spinner"></span>';
            const fd = new FormData();
            selectedFiles.forEach(f => fd.append('files', f));
            try {
                const resp = await fetch('/process-documents', { method: 'POST', body: fd });
                const data = await resp.json();
                let html = '<h3>📋 Results</h3>';
                for (let r of data.results) {
                    const ok = r.status === 'success';
                    html += `<div class="result-card ${ok ? 'success' : 'error'}">
                        <strong>${r.filename}</strong>
                        ${ok ? `<div>✂️ ${r.characters_extracted} chars | GraphRAG input: ${r.graphrag_input_ready}</div>
                                <div class="preview">${esc(r.preview)}</div>`
                             : `<div>❌ ${esc(r.message)}</div>`}
                    </div>`;
                }
                document.getElementById('uploadResults').innerHTML = html;
                alert(`✅ ${data.documents_processed} file(s) processed! Use the GraphRAG tab to rebuild the index.`);
                selectedFiles = []; renderFileList(); fileInput.value = '';
            } catch(err) { alert('Upload failed: ' + err.message); }
            finally { btn.disabled = false; btn.innerHTML = '🚀 Process Documents'; }
        });

        // ---- Query ----
        document.getElementById('queryBtn').addEventListener('click', async () => {
            const question = document.getElementById('questionInput').value.trim();
            const method   = document.getElementById('methodSelect').value;
            if (!question) return alert('Please enter a question');
            const btn = document.getElementById('queryBtn');
            const ansDiv = document.getElementById('queryAnswer');
            const errDiv = document.getElementById('queryError');
            btn.disabled = true; btn.innerHTML = 'Asking... <span class="spinner"></span>';
            ansDiv.style.display = 'none'; errDiv.innerText = '';
            try {
                const resp = await fetch('/rag/ask', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question, method })
                });
                const data = await resp.json();
                if (resp.ok) {
                    const isGraph = ['local','global','drift'].includes(data.method);
                    ansDiv.style.display = 'block';
                    ansDiv.innerHTML = `<strong>🤖 Answer <span class="badge ${isGraph ? 'graph' : 'vector'}">${data.method}</span></strong><br><br>${esc(data.answer)}`
                        + (data.retrieved_chunks != null ? `<div class="answer-meta">📚 ${data.retrieved_chunks} chunk(s) retrieved</div>` : '')
                        + `<div class="answer-meta">Source: ${data.source}</div>`;
                } else {
                    errDiv.innerText = `Error: ${data.detail || 'Unknown error'}`;
                }
            } catch(err) { errDiv.innerText = `Request failed: ${err.message}`; }
            finally { btn.disabled = false; btn.innerHTML = 'Ask'; }
        });

        // ---- GraphRAG index with polling ----
        let pollInterval = null;
        let currentJobId = null;
        let jobStartTime = null;
        
        function updateElapsedTime() {
            if (!jobStartTime) return;
            const elapsed = Math.floor((Date.now() - jobStartTime) / 1000);
            const minutes = Math.floor(elapsed / 60);
            const seconds = elapsed % 60;
            document.getElementById('jobElapsedTime').innerText = 
                `Elapsed: ${minutes}m ${seconds}s` + (elapsed > 900 ? ' ⚠️ Long running!' : '');
        }
        
        function showJobStatus(jobId, status) {
            currentJobId = jobId;
            const statusDisplay = document.getElementById('jobStatusDisplay');
            const cancelBtn = document.getElementById('cancelJobBtn');
            const indexBtn = document.getElementById('indexBtn');
            
            if (status === 'running') {
                statusDisplay.style.display = 'block';
                cancelBtn.style.display = 'inline-block';
                cancelBtn.onclick = () => cancelCurrentJob(jobId);
                indexBtn.disabled = true;
                
                if (!jobStartTime) jobStartTime = Date.now();
                
                // Update elapsed time every second
                if (pollInterval) clearInterval(pollInterval);
                const timeInterval = setInterval(updateElapsedTime, 1000);
                
                document.getElementById('jobStatusText').innerHTML = '🔄 Indexing in Progress...';
                document.getElementById('jobProgressIndicator').innerHTML = 
                    'Extracting entities → Building communities → Generating embeddings';
            } else {
                statusDisplay.style.display = 'none';
                cancelBtn.style.display = 'none';
                indexBtn.disabled = false;
                currentJobId = null;
                jobStartTime = null;
            }
        }
        
        async function cancelCurrentJob(jobId) {
            if (!confirm('Are you sure you want to cancel this indexing job?')) return;
            
            try {
                const resp = await fetch(`/graphrag/index/${jobId}`, { method: 'DELETE' });
                const data = await resp.json();
                
                if (resp.ok) {
                    if (pollInterval) clearInterval(pollInterval);
                    showJobStatus(null, 'cancelled');
                    document.getElementById('indexResult').innerHTML = 
                        `<strong>🛑 Job Cancelled</strong>\n\n${data.message}`;
                    alert('✅ Job cancelled successfully');
                } else {
                    alert('❌ Failed to cancel job: ' + (data.detail || 'Unknown error'));
                }
            } catch(err) {
                alert('❌ Failed to cancel job: ' + err.message);
            }
        }
        
        document.getElementById('indexBtn').addEventListener('click', async () => {
            const btn = document.getElementById('indexBtn');
            const resultDiv = document.getElementById('indexResult');
            btn.disabled = true; btn.innerHTML = 'Starting... <span class="spinner"></span>';
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = 'Initializing indexing job...';
            
            try {
                // Start the indexing job
                const resp = await fetch('/graphrag/index', { method: 'POST' });
                const data = await resp.json();
                
                if (data.status === 'already_running') {
                    alert('⚠️ Indexing is already in progress. Please wait for it to complete.');
                    btn.disabled = false;
                    btn.innerHTML = '🕸️ Build GraphRAG Index';
                    return;
                }
                
                if (!data.job_id) {
                    throw new Error('No job_id returned');
                }
                
                const jobId = data.job_id;
                jobStartTime = Date.now();
                showJobStatus(jobId, 'running');
                resultDiv.innerHTML = `<strong>Job ID:</strong> ${jobId}\n<strong>Status:</strong> running...\n\nIndexing in progress. This may take several minutes...`;
                
                // Poll for status
                pollInterval = setInterval(async () => {
                    try {
                        const statusResp = await fetch(`/graphrag/index/status/${jobId}`);
                        
                        // Handle 404 - job not found (cleaned up or never existed)
                        if (statusResp.status === 404) {
                            clearInterval(pollInterval);
                            btn.disabled = false;
                            btn.innerHTML = '🕸️ Build GraphRAG Index';
                            const errorData = await statusResp.json();
                            resultDiv.innerHTML = `<strong>❌ Job Not Found</strong>\n\n${esc(errorData.detail)}\n\n<em>The server may have restarted, or the job was cleaned up. Please start a new indexing job.</em>`;
                            alert('⚠️ Job not found. The server may have restarted. Please start a new indexing job.');
                            return;
                        }
                        
                        const statusData = await statusResp.json();
                        
                        if (statusData.status === 'running') {
                            resultDiv.innerHTML = `<strong>Job ID:</strong> ${jobId}\n<strong>Status:</strong> running... <span class="spinner"></span>\n\nIndexing in progress. Please wait...`;
                        } else if (statusData.status === 'completed') {
                            clearInterval(pollInterval);
                            btn.disabled = false;
                            btn.innerHTML = '🕸️ Build GraphRAG Index';
                            const stderrSection = statusData.stderr ? `\n\n<strong>Errors:</strong>\n${esc(statusData.stderr)}` : '';
                            resultDiv.innerHTML = `<strong>✅ Indexing Complete!</strong>\n<strong>Job ID:</strong> ${jobId}\n<strong>Index exists:</strong> ${statusData.index_exists}\n\n<strong>Output (last 3000 chars):</strong>\n${esc(statusData.stdout || 'No output')}${stderrSection}`;
                            alert('✅ GraphRAG indexing completed successfully!');
                        } else if (statusData.status === 'failed') {
                            clearInterval(pollInterval);
                            btn.disabled = false;
                            btn.innerHTML = '🕸️ Build GraphRAG Index';
                            resultDiv.innerHTML = `<strong>❌ Indexing Failed</strong>\n<strong>Job ID:</strong> ${jobId}\n\n<strong>Error:</strong>\n${esc(statusData.error || statusData.stderr || 'Unknown error')}`;
                            alert('❌ Indexing failed. Check the output for details.');
                        }
                    } catch(pollErr) {
                        console.error('Polling error:', pollErr);
                        // Stop polling on persistent errors
                        clearInterval(pollInterval);
                        btn.disabled = false;
                        btn.innerHTML = '🕸️ Build GraphRAG Index';
                        resultDiv.innerHTML = `<strong>❌ Polling Error</strong>\n\nFailed to check job status. The server may be unavailable.\n\n<em>Error: ${esc(pollErr.message)}</em>`;
                    }
                }, 3000); // Poll every 3 seconds
                
            } catch(err) { 
                alert('Failed to start indexing: ' + err.message);
                btn.disabled = false;
                btn.innerHTML = '🕸️ Build GraphRAG Index';
                resultDiv.style.display = 'none';
            }
        });

        document.getElementById('statusBtn').addEventListener('click', async () => {
            try {
                const resp = await fetch('/graphrag/status');
                const data = await resp.json();
                document.getElementById('indexResult').style.display = 'block';
                document.getElementById('indexResult').innerHTML =
                    `<strong>📊 GraphRAG Status</strong>\n\n` +
                    `Index exists: ${data.index_exists ? '✅ Yes' : '❌ No'}\n` +
                    `Input files: ${data.input_files_count}\n` +
                    `Output parquets: ${data.output_parquet_count}\n` +
                    `Root directory: ${data.root_directory}`;
            } catch(err) {
                alert('Failed to fetch status: ' + err.message);
            }
        });
        
        // ---- Visualize graph ----
        document.getElementById('visualizeBtn').addEventListener('click', async () => {
            const btn = document.getElementById('visualizeBtn');
            const resultDiv = document.getElementById('indexResult');
            btn.disabled = true; btn.innerHTML = 'Generating... <span class="spinner"></span>';
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = 'Creating interactive visualization...';
            
            try {
                const resp = await fetch('/graphrag/visualize', { method: 'POST' });
                const data = await resp.json();
                
                if (resp.ok) {
                    resultDiv.innerHTML = `<strong>✅ Visualization Created!</strong>\n\n<a href="${data.url}" target="_blank" style="color:#38bdf8;text-decoration:underline;cursor:pointer">🔗 Open Visualization in New Tab</a>\n\nFile: ${data.relative_path}`;
                } else {
                    resultDiv.innerHTML = `<strong>❌ Visualization Failed</strong>\n\n${esc(data.detail || 'Unknown error')}`;
                    alert('❌ Failed to generate visualization. Make sure the index is built first.');
                }
            } catch(err) {
                resultDiv.innerHTML = `<strong>❌ Error</strong>\n\n${esc(err.message)}`;
                alert('Failed to generate visualization: ' + err.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = '🎨 Visualize Graph';
            }
        });
        
        // ---- Reset stuck jobs ----
        document.getElementById('resetStuckJobsBtn').addEventListener('click', async () => {
            if (!confirm('Reset all jobs that have been running for more than 30 minutes?')) return;
            
            const btn = document.getElementById('resetStuckJobsBtn');
            const resultDiv = document.getElementById('indexResult');
            btn.disabled = true; btn.innerHTML = 'Resetting... <span class="spinner"></span>';
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = 'Checking for stuck jobs...';
            
            try {
                const resp = await fetch('/graphrag/reset-stuck-jobs', { method: 'POST' });
                const data = await resp.json();
                
                if (resp.ok) {
                    if (data.reset_count === 0) {
                        resultDiv.innerHTML = `<strong>ℹ️ No Stuck Jobs Found</strong>\n\n${data.message}\n\nAll jobs are either completed or running normally.`;
                        alert('✅ No stuck jobs found. You are all set!');
                    } else {
                        let jobsList = '';
                        if (data.jobs_reset && data.jobs_reset.length > 0) {
                            jobsList = `\n\n<strong>Reset Jobs:</strong>\n`;
                            data.jobs_reset.forEach(job => {
                                const minutes = Math.floor(job.runtime_seconds / 60);
                                jobsList += `• ${job.job_id.substring(0, 8)}... (ran for ${minutes} min)\n`;
                            });
                        }
                        resultDiv.innerHTML = `<strong>✅ Stuck Jobs Reset!</strong>\n\n${data.message}${jobsList}\n\nYou can now start a new indexing job.`;

                        alert(`✅ Reset ${data.reset_count} stuck job(s). You can now start indexing again!`);
                    }
                } else {
                    resultDiv.innerHTML = `<strong>❌ Reset Failed</strong>\n\n${esc(data.detail || 'Unknown error')}`;
                    alert('❌ Failed to reset jobs');
                }
            } catch(err) {
                resultDiv.innerHTML = `<strong>❌ Error</strong>\n\n${esc(err.message)}`;
                alert('Failed to reset jobs: ' + err.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = '🔄 Reset Stuck Jobs';
            }
        });
        
        // Clean up polling on page unload
        window.addEventListener('beforeunload', () => {
            if (pollInterval) clearInterval(pollInterval);
        });

        // ---- Processing History ----
        document.getElementById('refreshHistoryBtn').addEventListener('click', loadHistory);
        document.getElementById('historyFilter').addEventListener('change', loadHistory);

        async function loadHistory() {
            const filter = document.getElementById('historyFilter').value;
            const url = filter ? `/documents/history?status=${filter}&limit=100` : '/documents/history?limit=100';
            
            try {
                const resp = await fetch(url);
                const data = await resp.json();
                
                let html = `<p style="color:#94a3b8;margin-bottom:1rem">Total: ${data.total} records</p>`;
                
                data.history.forEach(h => {
                    const statusColor = h.status === 'success' ? '#10b981' : h.status === 'failed' ? '#ef4444' : '#f59e0b';
                    const statusIcon = h.status === 'success' ? '✅' : h.status === 'failed' ? '❌' : '⏳';
                    
                    html += `<div class="result-card" style="border-left-color:${statusColor}">
                        <div style="display:flex;justify-content:space-between;align-items:start">
                            <div>
                                <strong>${statusIcon} ${h.filename}</strong>
                                <div style="color:#94a3b8;font-size:0.85rem;margin-top:0.25rem">
                                    ID: ${h.id} | ${h.file_type} | ${(h.file_size/1024).toFixed(1)} KB
                                </div>
                            </div>
                            <div style="text-align:right">
                                <div style="color:${statusColor};font-weight:bold">${h.status.toUpperCase()}</div>
                                <div style="color:#94a3b8;font-size:0.85rem">${new Date(h.created_at).toLocaleString()}</div>
                            </div>
                        </div>
                        ${h.status === 'success' ? `
                            <div style="margin-top:0.5rem;color:#94a3b8;font-size:0.9rem">
                                📝 ${h.characters_extracted?.toLocaleString() || 0} chars | 
                                ✂️ ${h.chunks_created || 0} chunks | 
                                ⏱️ ${h.processing_time_ms || 0}ms
                            </div>
                        ` : ''}
                        ${h.error_message ? `<div style="margin-top:0.5rem;color:#f87171;font-size:0.85rem">Error: ${esc(h.error_message)}</div>` : ''}
                    </div>`;
                });
                
                document.getElementById('historyResults').innerHTML = html || '<p style="color:#94a3b8">No records found</p>';
            } catch(err) {
                document.getElementById('historyResults').innerHTML = `<p style="color:#f87171">Failed to load history: ${err.message}</p>`;
            }
        }

        document.getElementById('getStatsBtn').addEventListener('click', async () => {
            try {
                const resp = await fetch('/documents/stats?days=7');
                const data = await resp.json();
                
                let html = '<h3 style="color:#38bdf8;margin-bottom:1rem">Last 7 Days Statistics</h3>';
                
                if (data.daily_stats && data.daily_stats.length > 0) {
                    html += '<div style="background:#1e293b;padding:1rem;border-radius:0.5rem;margin-bottom:1rem">';
                    html += '<h4 style="color:#38bdf8;margin-bottom:0.5rem">Daily Breakdown</h4>';
                    data.daily_stats.forEach(day => {
                        const successRate = day.total > 0 ? ((day.successful / day.total) * 100).toFixed(1) : 0;
                        html += `<div style="padding:0.5rem;border-bottom:1px solid #334155">
                            <strong>${day.date}</strong>: ${day.total} docs | 
                            ✅ ${day.successful} | ❌ ${day.failed} | 
                            Success Rate: ${successRate}% | 
                            Avg Time: ${day.avg_time ? Math.round(day.avg_time) : 0}ms
                        </div>`;
                    });
                    html += '</div>';
                }
                
                if (data.file_types && data.file_types.length > 0) {
                    html += '<div style="background:#1e293b;padding:1rem;border-radius:0.5rem">';
                    html += '<h4 style="color:#38bdf8;margin-bottom:0.5rem">By File Type</h4>';
                    data.file_types.forEach(ft => {
                        html += `<div style="padding:0.5rem;border-bottom:1px solid #334155">
                            <strong>${ft.file_type}</strong>: ${ft.count} files | 
                            Avg Time: ${ft.avg_time ? Math.round(ft.avg_time) : 0}ms
                        </div>`;
                    });
                    html += '</div>';
                }
                
                document.getElementById('historyResults').innerHTML = html;
            } catch(err) {
                alert('Failed to load statistics: ' + err.message);
            }
        });

        // ---- API Keys Management ----
        document.getElementById('generateKeyBtn').addEventListener('click', async () => {
            const keyName = document.getElementById('keyNameInput').value.trim();
            const rateLimit = document.getElementById('rateLimitInput').value;
            
            if (!keyName) return alert('Please enter a key name');
            
            try {
                const resp = await fetch(`/auth/keys?key_name=${encodeURIComponent(keyName)}&rate_limit=${rateLimit}`, {
                    method: 'POST'
                });
                const data = await resp.json();
                
                if (resp.ok) {
                    document.getElementById('newKeyDisplay').style.display = 'block';
                    document.getElementById('newKeyValue').innerText = data.api_key;
                    document.getElementById('keyNameInput').value = '';
                    
                    // Refresh keys list
                    loadAPIKeys();
                } else {
                    alert('Failed to generate key: ' + (data.detail || 'Unknown error'));
                }
            } catch(err) {
                alert('Failed to generate key: ' + err.message);
            }
        });

        async function loadAPIKeys() {
            try {
                const resp = await fetch('/auth/keys');
                const data = await resp.json();
                
                let html = '';
                if (data.keys && data.keys.length > 0) {
                    data.keys.forEach(key => {
                        const activeColor = key.is_active ? '#10b981' : '#94a3b8';
                        const activeText = key.is_active ? '✅ Active' : '❌ Inactive';
                        const expiredText = key.expires_at && new Date(key.expires_at) < new Date() ? '⏰ EXPIRED' : '';
                        
                        html += `<div class="result-card" style="border-left-color:${activeColor};margin-bottom:1rem">
                            <div style="display:flex;justify-content:space-between;align-items:start">
                                <div>
                                    <strong>${key.key_name}</strong>
                                    <div style="color:#94a3b8;font-size:0.85rem;margin-top:0.25rem">
                                        ID: ${key.id} | Created: ${new Date(key.created_at).toLocaleDateString()}
                                    </div>
                                    <div style="margin-top:0.5rem;font-size:0.9rem">
                                        <span style="color:${activeColor}">${activeText}</span> ${expiredText} |
                                        Rate Limit: ${key.rate_limit}/hr | 
                                        Usage: ${key.usage_count || 0} requests
                                    </div>
                                    ${key.last_used_at ? `<div style="color:#94a3b8;font-size:0.85rem">Last used: ${new Date(key.last_used_at).toLocaleString()}</div>` : ''}
                                    ${key.expires_at ? `<div style="color:#94a3b8;font-size:0.85rem">Expires: ${new Date(key.expires_at).toLocaleString()}</div>` : ''}
                                </div>
                                ${key.is_active ? `<button onclick="revokeKey(${key.id})" style="background:#ef4444">🗑️ Revoke</button>` : ''}
                            </div>
                        </div>`;
                    });
                } else {
                    html = '<p style="color:#94a3b8">No API keys found. Generate your first key above.</p>';
                }
                
                document.getElementById('keysListResults').innerHTML = html;
            } catch(err) {
                document.getElementById('keysListResults').innerHTML = `<p style="color:#f87171">Failed to load keys: ${err.message}</p>`;
            }
        }

        async function revokeKey(keyId) {
            if (!confirm('Are you sure you want to revoke this API key? This action cannot be undone.')) return;
            
            try {
                const resp = await fetch(`/auth/keys/${keyId}`, { method: 'DELETE' });
                if (resp.ok) {
                    alert('✅ Key revoked successfully');
                    loadAPIKeys();
                } else {
                    const data = await resp.json();
                    alert('Failed to revoke key: ' + (data.detail || 'Unknown error'));
                }
            } catch(err) {
                alert('Failed to revoke key: ' + err.message);
            }
        }

        // ---- Metrics ----
        document.getElementById('refreshMetricsBtn').addEventListener('click', loadMetrics);

        async function loadMetrics() {
            try {
                const resp = await fetch('/metrics');
                const data = await resp.json();
                
                // Check if there was an error
                if (data.error || data.data_available === false) {
                    document.getElementById('metricsResults').innerHTML = `
                        <div class="result-card" style="border-left-color:#ef4444">
                            <h3 style="color:#ef4444;margin-bottom:0.5rem">❌ Failed to Load Metrics</h3>
                            <p style="color:#94a3b8">${data.message || data.error || 'Unknown error occurred'}</p>
                            <p style="color:#94a3b8;margin-top:1rem;font-size:0.9rem">
                                This usually happens when database tables don't exist yet or there's a connection issue.
                                Try uploading and processing some documents first.
                            </p>
                        </div>`;
                    return;
                }
                
                let html = `<div style="color:#94a3b8;margin-bottom:1rem">Last updated: ${new Date(data.timestamp).toLocaleString()}</div>`;
                
                // Database metrics
                html += `<div class="result-card" style="border-left-color:#3b82f6;margin-bottom:1rem">
                    <h3 style="color:#38bdf8;margin-bottom:0.5rem">🗄️ Database</h3>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem">
                        <div><strong>Total Documents:</strong> ${data.database.total_documents.toLocaleString()}</div>
                        <div><strong>Pool Size:</strong> ${data.database.pool_size}</div>
                        <div><strong>Free Connections:</strong> ${data.database.pool_free_connections}</div>
                        <div><strong>Used Connections:</strong> ${data.database.pool_used_connections}</div>
                    </div>
                </div>`;
                
                // Processing metrics
                html += `<div class="result-card" style="border-left-color:#10b981;margin-bottom:1rem">
                    <h3 style="color:#38bdf8;margin-bottom:0.5rem">📝 Processing (24h)</h3>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem">
                        <div><strong>Total:</strong> ${data.processing.total_24h}</div>
                        <div><strong>Successful:</strong> ${data.processing.successful_24h}</div>
                        <div><strong>Failed:</strong> ${data.processing.failed_24h}</div>
                        <div><strong>Avg Time:</strong> ${Math.round(data.processing.avg_time_ms)}ms</div>
                    </div>
                </div>`;
                
                // API metrics
                html += `<div class="result-card" style="border-left-color:#6366f1;margin-bottom:1rem">
                    <h3 style="color:#38bdf8;margin-bottom:0.5rem">🌐 API (1h)</h3>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem">
                        <div><strong>Requests:</strong> ${data.api.requests_1h}</div>
                        <div><strong>Avg Response:</strong> ${Math.round(data.api.avg_response_time_ms)}ms</div>
                        <div><strong>Errors:</strong> ${data.api.errors_1h}</div>
                        <div><strong>Rate Limited:</strong> ${data.api.rate_limited_1h}</div>
                    </div>
                </div>`;
                
                // System metrics
                html += `<div class="result-card" style="border-left-color:#f59e0b;margin-bottom:1rem">
                    <h3 style="color:#38bdf8;margin-bottom:0.5rem">💻 System</h3>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem">
                        <div><strong>CPU:</strong> ${data.system.cpu_percent.toFixed(1)}%</div>
                        <div><strong>Memory:</strong> ${data.system.memory_percent.toFixed(1)}%</div>
                        <div><strong>Disk:</strong> ${data.system.disk_percent.toFixed(1)}%</div>
                        <div><strong>Uptime:</strong> ${Math.round(data.application.uptime_seconds / 3600)}h</div>
                    </div>
                </div>`;
                
                // GraphRAG metrics
                html += `<div class="result-card" style="border-left-color:#7c3aed;margin-bottom:1rem">
                    <h3 style="color:#38bdf8;margin-bottom:0.5rem">🕸️ GraphRAG</h3>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem">
                        <div><strong>Index Exists:</strong> ${data.graphrag.index_exists ? '✅ Yes' : '❌ No'}</div>
                        <div><strong>Input Files:</strong> ${data.graphrag.input_files}</div>
                        <div><strong>Entities:</strong> ${data.graphrag.entities_count.toLocaleString()}</div>
                        <div><strong>Relationships:</strong> ${data.graphrag.relationships_count.toLocaleString()}</div>
                    </div>
                </div>`;
                
                document.getElementById('metricsResults').innerHTML = html;
            } catch(err) {
                document.getElementById('metricsResults').innerHTML = `<p style="color:#f87171">Failed to load metrics: ${err.message}</p>`;
            }
        }

        document.getElementById('downloadPrometheusBtn').addEventListener('click', async () => {
            try {
                const resp = await fetch('/metrics/prometheus');
                const text = await resp.text();
                
                const blob = new Blob([text], { type: 'text/plain' });
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'metrics.txt';
                a.click();
                window.URL.revokeObjectURL(url);
            } catch(err) {
                alert('Failed to download: ' + err.message);
            }
        });

        // Load initial data when tabs are opened
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                const id = tab.dataset.tab;
                if (id === 'history') loadHistory();
                if (id === 'apikeys') loadAPIKeys();
                if (id === 'metrics') loadMetrics();
            });
        });

        function esc(s) { if (!s) return ''; return s.replace(/[&<>]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;'})[m]); }
    </script>
    </body>
    </html>
    """

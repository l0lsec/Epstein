"""
Epstein Files Search Platform - API Server
FastAPI backend for document search and LLM-powered analysis
"""

import os
import json
import asyncio
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from database import Database, VectorStore, build_index
from llm import LLMAssistant
from security_logger import (
    SecurityLogger, 
    RequestLoggingMiddleware, 
    get_security_logger,
    get_client_info
)


# Configuration
BASE_PATH = Path(os.getenv("EPSTEIN_BASE_PATH", Path(__file__).parent.parent))
DB_PATH = BASE_PATH / "epstein.db"
VECTOR_PATH = BASE_PATH / "vector_store"
STATIC_PATH = BASE_PATH / "frontend"

# Auto-indexing configuration (in seconds)
AUTO_INDEX_INTERVAL = int(os.getenv("AUTO_INDEX_INTERVAL", "172800"))  # Default: 24 hour
AUTO_INDEX_ENABLED = os.getenv("AUTO_INDEX_ENABLED", "true").lower() == "true"

# Global instances
db: Optional[Database] = None
vector_store: Optional[VectorStore] = None
llm: Optional[LLMAssistant] = None
index_task: Optional[asyncio.Task] = None
last_index_time: Optional[datetime] = None
is_indexing: bool = False

# Initialize security logger
security_logger = get_security_logger()


async def auto_index_task():
    """Background task to periodically check for new files and rebuild index"""
    global db, vector_store, last_index_time, is_indexing
    
    while True:
        try:
            await asyncio.sleep(AUTO_INDEX_INTERVAL)
            
            if is_indexing:
                security_logger.log_system_event(
                    "index_skip",
                    "Auto-index skipped: already indexing"
                )
                continue
            
            security_logger.log_index_operation(
                operation="start",
                trigger="auto"
            )
            is_indexing = True
            start_time = datetime.now()
            
            # Run indexing in a thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: build_index(str(BASE_PATH)))
            
            # Reload database and vector store
            if DB_PATH.exists():
                db = Database(str(DB_PATH))
            if VECTOR_PATH.exists():
                vector_store = VectorStore(str(VECTOR_PATH))
            
            last_index_time = datetime.now()
            is_indexing = False
            
            duration = (last_index_time - start_time).total_seconds()
            stats = db.get_stats() if db else {}
            
            security_logger.log_index_operation(
                operation="complete",
                trigger="auto",
                duration_seconds=duration,
                document_count=stats.get('total_documents', 0)
            )
            
        except asyncio.CancelledError:
            security_logger.log_system_event("index_cancelled", "Auto-index task cancelled")
            break
        except Exception as e:
            is_indexing = False
            security_logger.log_error(
                error=e,
                context="auto_index_task"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup"""
    global db, vector_store, llm, index_task, last_index_time
    
    security_logger.log_system_event(
        "startup",
        "Initializing Epstein Files Search Platform",
        base_path=str(BASE_PATH)
    )
    
    # Initialize database
    if DB_PATH.exists():
        db = Database(str(DB_PATH))
        doc_count = db.get_stats()['total_documents']
        security_logger.log_system_event(
            "database_loaded",
            f"Database loaded with {doc_count} documents",
            document_count=doc_count
        )
    else:
        security_logger.log_system_event(
            "database_missing",
            "Database not found. Run indexing first.",
            severity="warning"
        )
    
    # Initialize vector store
    if VECTOR_PATH.exists():
        vector_store = VectorStore(str(VECTOR_PATH))
        chunk_count = vector_store.get_count()
        security_logger.log_system_event(
            "vector_store_loaded",
            f"Vector store loaded with {chunk_count} chunks",
            chunk_count=chunk_count
        )
    else:
        security_logger.log_system_event(
            "vector_store_missing",
            "Vector store not found. Run indexing first.",
            severity="warning"
        )
    
    # Initialize LLM
    llm = LLMAssistant()
    if llm.is_available():
        security_logger.log_system_event("llm_ready", "LLM assistant initialized")
    else:
        security_logger.log_system_event(
            "llm_unavailable",
            "LLM not configured (set OPENAI_API_KEY)",
            severity="warning"
        )
    
    # Check reCAPTCHA configuration
    if RECAPTCHA_SECRET_KEY:
        security_logger.log_system_event("recaptcha_ready", "reCAPTCHA spam protection enabled")
    else:
        security_logger.log_system_event(
            "recaptcha_disabled",
            "reCAPTCHA not configured (set RECAPTCHA_SECRET_KEY) - feedback spam protection disabled",
            severity="warning"
        )
    
    # Start auto-indexing background task
    last_index_time = datetime.now()
    if AUTO_INDEX_ENABLED:
        index_task = asyncio.create_task(auto_index_task())
        interval_mins = AUTO_INDEX_INTERVAL // 60
        security_logger.log_system_event(
            "auto_index_enabled",
            f"Auto-indexing enabled (every {interval_mins} minutes)",
            interval_seconds=AUTO_INDEX_INTERVAL
        )
    else:
        security_logger.log_system_event("auto_index_disabled", "Auto-indexing is disabled")
    
    yield
    
    # Cleanup
    security_logger.log_system_event("shutdown", "Server shutting down")
    if index_task:
        index_task.cancel()
        try:
            await index_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Epstein Files Search Platform",
    description="Search and analyze the Epstein document archive with AI assistance",
    version="1.0.0",
    lifespan=lifespan
)

# CORS for frontend - configurable origins
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Security logging middleware - logs all requests with timing and security checks
app.add_middleware(RequestLoggingMiddleware, security_logger=security_logger)


# Security headers middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    # Allow framing for document file endpoints (PDF viewer uses iframe)
    # Block framing for all other pages to prevent clickjacking
    if path.endswith("/file") and "/api/documents/" in path:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    
    return response


# Admin API key for protected endpoints
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")


# Request/Response Models
class SearchRequest(BaseModel):
    query: str
    search_type: str = "hybrid"  # "fulltext", "semantic", "hybrid"
    category: Optional[str] = None
    subcategory: Optional[str] = None
    file_type: Optional[str] = None  # "pdf", "audio", "video"
    limit: int = 50  # Results per page (unlimited total via pagination)
    offset: int = 0


class AskRequest(BaseModel):
    question: str
    category: Optional[str] = None
    num_context_docs: int = 5


class SearchResult(BaseModel):
    id: str
    filename: str
    path: str
    category: str
    subcategory: str
    snippet: Optional[str] = None
    score: Optional[float] = None
    page_count: Optional[int] = None


class StatsResponse(BaseModel):
    total_documents: int
    total_pages: int
    by_category: List[dict]
    by_subcategory: List[dict]
    by_file_type: List[dict]
    vector_chunks: int
    llm_available: bool


class IndexStatusResponse(BaseModel):
    auto_index_enabled: bool
    auto_index_interval_seconds: int
    is_indexing: bool
    last_index_time: Optional[str] = None
    next_index_time: Optional[str] = None


class FeedbackRequest(BaseModel):
    type: str  # "bug", "feature", "content", "other"
    email: Optional[str] = None
    message: str
    recaptcha_token: Optional[str] = None  # reCAPTCHA response token
    _ts: Optional[str] = None  # Timestamp for spam protection


# reCAPTCHA secret key (loaded from environment variable)
RECAPTCHA_SECRET_KEY = os.getenv("RECAPTCHA_SECRET_KEY")


# Feedback storage path
FEEDBACK_PATH = BASE_PATH / "feedback.json"

# Rate limiting for feedback (IP -> last submission time)
feedback_rate_limit: dict = {}
FEEDBACK_RATE_LIMIT_SECONDS = 60  # One submission per minute per IP


# API Routes

@app.get("/")
async def root():
    """Serve the frontend"""
    index_path = STATIC_PATH / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "Epstein Files Search Platform API", "docs": "/docs"}


@app.get("/robots.txt")
async def robots_txt():
    """Serve robots.txt for SEO"""
    robots_path = STATIC_PATH / "robots.txt"
    if robots_path.exists():
        return FileResponse(robots_path, media_type="text/plain")
    return FileResponse(robots_path, status_code=404)


@app.get("/sitemap.xml")
async def sitemap_xml():
    """Serve sitemap.xml for SEO"""
    sitemap_path = STATIC_PATH / "sitemap.xml"
    if sitemap_path.exists():
        return FileResponse(sitemap_path, media_type="application/xml")
    return FileResponse(sitemap_path, status_code=404)


@app.get("/api/stats")
async def get_stats() -> StatsResponse:
    """Get platform statistics"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    stats = db.get_stats()
    return StatsResponse(
        total_documents=stats["total_documents"],
        total_pages=stats["total_pages"],
        by_category=stats["by_category"],
        by_subcategory=stats["by_subcategory"],
        by_file_type=stats.get("by_file_type", []),
        vector_chunks=vector_store.get_count() if vector_store else 0,
        llm_available=llm.is_available() if llm else False
    )


@app.get("/api/index/status")
async def get_index_status() -> IndexStatusResponse:
    """Get the current indexing status"""
    from datetime import timedelta
    
    next_index = None
    if AUTO_INDEX_ENABLED and last_index_time:
        next_time = last_index_time + timedelta(seconds=AUTO_INDEX_INTERVAL)
        next_index = next_time.isoformat()
    
    return IndexStatusResponse(
        auto_index_enabled=AUTO_INDEX_ENABLED,
        auto_index_interval_seconds=AUTO_INDEX_INTERVAL,
        is_indexing=is_indexing,
        last_index_time=last_index_time.isoformat() if last_index_time else None,
        next_index_time=next_index
    )


@app.post("/api/index/trigger")
async def trigger_index(request: Request, x_api_key: str = Header(None)):
    """Manually trigger a re-index (requires ADMIN_API_KEY)"""
    global db, vector_store, last_index_time, is_indexing
    
    client_ip, request_id = get_client_info(request)
    
    # Check admin API key if configured
    if ADMIN_API_KEY and x_api_key != ADMIN_API_KEY:
        security_logger.log_security_event(
            event_type="unauthorized_index_attempt",
            severity="high",
            client_ip=client_ip,
            message="Unauthorized index trigger attempt",
            request_id=request_id
        )
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if is_indexing:
        security_logger.log_system_event(
            "index_rejected",
            f"Manual index trigger rejected - already in progress",
            client_ip=client_ip
        )
        raise HTTPException(status_code=409, detail="Indexing already in progress")
    
    is_indexing = True
    start_time = datetime.now()
    
    security_logger.log_index_operation(
        operation="start",
        trigger="manual",
        triggered_by_ip=client_ip
    )
    
    try:
        # Run indexing in background
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: build_index(str(BASE_PATH)))
        
        # Reload stores
        if DB_PATH.exists():
            db = Database(str(DB_PATH))
        if VECTOR_PATH.exists():
            vector_store = VectorStore(str(VECTOR_PATH))
        
        last_index_time = datetime.now()
        is_indexing = False
        
        duration = (last_index_time - start_time).total_seconds()
        stats = db.get_stats() if db else {}
        
        security_logger.log_index_operation(
            operation="complete",
            trigger="manual",
            duration_seconds=duration,
            document_count=stats.get("total_documents", 0),
            triggered_by_ip=client_ip
        )
        
        return {
            "success": True,
            "message": f"Index rebuilt successfully",
            "total_documents": stats.get("total_documents", 0),
            "indexed_at": last_index_time.isoformat()
        }
    except Exception as e:
        is_indexing = False
        security_logger.log_error(
            error=e,
            context="manual_index_trigger",
            client_ip=client_ip,
            request_id=request_id
        )
        raise HTTPException(status_code=500, detail="An internal error occurred during indexing")


@app.post("/api/index/rebuild-fts")
async def rebuild_fts_index(request: Request, x_api_key: str = Header(None)):
    """Rebuild the FTS5 full-text search index to fix sync issues (requires ADMIN_API_KEY)"""
    global db
    
    client_ip, request_id = get_client_info(request)
    
    # Check admin API key
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API key not configured")
    
    if x_api_key != ADMIN_API_KEY:
        security_logger.log_security_event(
            event_type="invalid_admin_key",
            severity="high",
            client_ip=client_ip,
            message="Invalid API key for FTS rebuild",
            request_id=request_id
        )
        raise HTTPException(status_code=403, detail="Invalid API key")
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    try:
        security_logger.log_system_event(
            "fts_rebuild_start",
            f"FTS index rebuild triggered from {client_ip}"
        )
        
        db.rebuild_fts()
        
        security_logger.log_system_event(
            "fts_rebuild_complete",
            "FTS index rebuilt successfully"
        )
        
        return {
            "success": True,
            "message": "FTS index rebuilt successfully"
        }
    except Exception as e:
        security_logger.log_error(
            error=e,
            context="fts_rebuild",
            client_ip=client_ip,
            request_id=request_id
        )
        raise HTTPException(status_code=500, detail=f"Failed to rebuild FTS index: {str(e)}")


@app.get("/api/categories")
async def get_categories():
    """Get all document categories"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    stats = db.get_stats()
    return {"categories": stats["by_category"]}


@app.get("/api/subcategories")
async def get_subcategories(category: Optional[str] = None):
    """Get subcategories, optionally filtered by category"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    stats = db.get_stats()
    subcategories = stats["by_subcategory"]
    
    if category:
        subcategories = [s for s in subcategories if s.get("category") == category]
    
    return {"subcategories": subcategories}


@app.post("/api/feedback")
async def submit_feedback(feedback: FeedbackRequest, request: Request):
    """Submit user feedback with spam protection"""
    import httpx
    
    # Get client info for logging
    client_ip, request_id = get_client_info(request)
    
    # Verify reCAPTCHA (skip if secret key not configured)
    if RECAPTCHA_SECRET_KEY:
        if not feedback.recaptcha_token:
            security_logger.log_validation_failure(
                client_ip=client_ip,
                endpoint="/api/feedback",
                field="recaptcha_token",
                reason="Missing reCAPTCHA token",
                request_id=request_id
            )
            raise HTTPException(status_code=400, detail="Please complete the reCAPTCHA verification.")
        
        try:
            async with httpx.AsyncClient() as client:
                recaptcha_response = await client.post(
                    "https://www.google.com/recaptcha/api/siteverify",
                    data={
                        "secret": RECAPTCHA_SECRET_KEY,
                        "response": feedback.recaptcha_token
                    }
                )
                recaptcha_result = recaptcha_response.json()
                
                if not recaptcha_result.get("success"):
                    security_logger.log_recaptcha_failure(
                        client_ip=client_ip,
                        reason="Verification failed",
                        request_id=request_id
                    )
                    raise HTTPException(status_code=400, detail="reCAPTCHA verification failed. Please try again.")
                
                # v3 returns a score (0.0 - 1.0), reject if too low (likely bot)
                score = recaptcha_result.get("score", 1.0)
                if score < 0.3:
                    security_logger.log_recaptcha_failure(
                        client_ip=client_ip,
                        reason="Low score (bot suspected)",
                        score=score,
                        request_id=request_id
                    )
                    raise HTTPException(status_code=400, detail="Suspicious activity detected. Please try again later.")
                
                security_logger.log_security_event(
                    event_type="recaptcha_success",
                    severity="low",
                    client_ip=client_ip,
                    message=f"reCAPTCHA passed with score {score}",
                    request_id=request_id,
                    score=score
                )
        except httpx.RequestError as e:
            # If reCAPTCHA verification fails due to network, allow submission but log it
            security_logger.log_error(
                error=e,
                context="recaptcha_verification",
                client_ip=client_ip,
                request_id=request_id
            )
    
    # Rate limiting check
    now = datetime.now()
    if client_ip in feedback_rate_limit:
        last_submission = feedback_rate_limit[client_ip]
        elapsed = (now - last_submission).total_seconds()
        if elapsed < FEEDBACK_RATE_LIMIT_SECONDS:
            remaining = int(FEEDBACK_RATE_LIMIT_SECONDS - elapsed)
            security_logger.log_rate_limit_exceeded(
                client_ip=client_ip,
                endpoint="/api/feedback",
                limit=1,
                window_seconds=FEEDBACK_RATE_LIMIT_SECONDS,
                request_id=request_id
            )
            raise HTTPException(
                status_code=429, 
                detail=f"Please wait {remaining} seconds before submitting again."
            )
    
    # Basic validation
    if not feedback.message or len(feedback.message.strip()) < 10:
        security_logger.log_validation_failure(
            client_ip=client_ip,
            endpoint="/api/feedback",
            field="message",
            reason="Message too short (< 10 chars)",
            request_id=request_id
        )
        raise HTTPException(status_code=400, detail="Message must be at least 10 characters.")
    
    if len(feedback.message) > 5000:
        security_logger.log_validation_failure(
            client_ip=client_ip,
            endpoint="/api/feedback",
            field="message",
            reason="Message too long (> 5000 chars)",
            request_id=request_id
        )
        raise HTTPException(status_code=400, detail="Message is too long (max 5000 characters).")
    
    # Update rate limit
    feedback_rate_limit[client_ip] = now
    
    feedback_entry = {
        "id": now.strftime("%Y%m%d%H%M%S%f"),
        "timestamp": now.isoformat(),
        "type": feedback.type,
        "email": feedback.email,
        "message": feedback.message,
        "ip": client_ip[:20]  # Store partial IP for reference
    }
    
    # Load existing feedback
    feedback_list = []
    if FEEDBACK_PATH.exists():
        try:
            with open(FEEDBACK_PATH, 'r') as f:
                feedback_list = json.load(f)
        except (json.JSONDecodeError, IOError):
            feedback_list = []
    
    # Append new feedback
    feedback_list.append(feedback_entry)
    
    # Save feedback
    try:
        with open(FEEDBACK_PATH, 'w') as f:
            json.dump(feedback_list, f, indent=2)
        
        security_logger.log_feedback_submission(
            client_ip=client_ip,
            feedback_type=feedback.type,
            feedback_id=feedback_entry["id"],
            request_id=request_id
        )
        
        return {
            "success": True,
            "message": "Feedback submitted successfully",
            "id": feedback_entry["id"]
        }
    except IOError as e:
        security_logger.log_error(
            error=e,
            context="feedback_save",
            client_ip=client_ip,
            request_id=request_id
        )
        raise HTTPException(status_code=500, detail="An internal error occurred while saving feedback")


@app.post("/api/search")
async def search(search_request: SearchRequest, request: Request) -> dict:
    """Search documents with full-text, semantic, or hybrid search"""
    client_ip, request_id = get_client_info(request)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    results = []
    total_count = 0
    
    if search_request.search_type in ["fulltext", "hybrid"]:
        # Full-text search
        try:
            ft_results = db.search_fulltext(
                query=search_request.query,
                limit=search_request.limit,
                offset=search_request.offset,
                category=search_request.category,
                subcategory=search_request.subcategory,
                file_type=search_request.file_type
            )
            # Get actual total count for pagination
            total_count = db.count_fulltext_results(
                query=search_request.query,
                category=search_request.category,
                subcategory=search_request.subcategory,
                file_type=search_request.file_type
            )
            for r in ft_results:
                results.append({
                    **r,
                    "search_type": "fulltext",
                    "score": abs(r.get("score", 0))
                })
        except Exception as e:
            security_logger.log_error(
                error=e,
                context="fulltext_search",
                client_ip=client_ip,
                request_id=request_id,
                query=search_request.query[:100]
            )
            if search_request.search_type == "fulltext":
                raise HTTPException(status_code=400, detail="Search query error. Please check your search syntax.")
    
    if search_request.search_type in ["semantic", "hybrid"] and vector_store:
        # Semantic search (doesn't support pagination as well, so we use it for discovery)
        sem_results = vector_store.search(
            query=search_request.query,
            n_results=search_request.limit,
            category=search_request.category
        )
        
        # Merge with existing results or add new
        # Note: Validate docs exist in DB to avoid stale vector store entries
        existing_ids = {r["id"] for r in results}
        for r in sem_results:
            doc_id = r.get("id", "")
            if doc_id and doc_id not in existing_ids:
                # Verify document exists in database (vector store may have stale entries)
                doc = db.get_document(doc_id)
                if doc:
                    text = r.get("text", "")
                    results.append({
                        "id": doc_id,
                        "filename": doc.get("filename", r.get("filename", "Unknown")),
                        "path": doc.get("path", r.get("path", "")),
                        "category": doc.get("category", r.get("category", "Unknown")),
                        "subcategory": doc.get("subcategory", r.get("subcategory", "")),
                        "file_type": doc.get("file_type", "pdf"),
                        "page_count": doc.get("page_count"),
                        "duration_seconds": doc.get("duration_seconds"),
                        "snippet": text[:300] + "..." if len(text) > 300 else text,
                        "search_type": "semantic",
                        "score": r.get("score", 0)
                    })
                    existing_ids.add(doc_id)
        
        # For semantic/hybrid, total is approximate since vector search doesn't have exact count
        if search_request.search_type == "semantic":
            total_count = len(results)
        # For hybrid, use fulltext total as the authoritative count
    
    # Sort by score (results are already paginated for fulltext)
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    
    # Get faceted counts for filter dropdowns
    facets = {}
    if search_request.search_type in ["fulltext", "hybrid"]:
        try:
            facets = db.get_search_facets(
                query=search_request.query,
                category=search_request.category,
                subcategory=search_request.subcategory,
                file_type=search_request.file_type
            )
        except Exception as e:
            security_logger.log_error(
                error=e,
                context="search_facets",
                client_ip=client_ip,
                request_id=request_id
            )
            # Non-fatal: continue without facets
    
    # Log the search query for audit
    security_logger.log_search_query(
        client_ip=client_ip,
        query=search_request.query,
        search_type=search_request.search_type,
        result_count=len(results),
        request_id=request_id,
        category=search_request.category,
        file_type=search_request.file_type
    )
    
    return {
        "query": search_request.query,
        "search_type": search_request.search_type,
        "total": total_count,
        "offset": search_request.offset,
        "limit": search_request.limit,
        "results": results,
        "facets": facets
    }


@app.get("/api/documents")
async def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    file_type: Optional[str] = None,
    filename: Optional[str] = None
):
    """List all documents with pagination"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    docs = db.get_all_documents(limit=limit, offset=offset, category=category, subcategory=subcategory, file_type=file_type, filename=filename)
    total = db.count_documents(category=category, subcategory=subcategory, file_type=file_type, filename=filename)
    
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "documents": docs
    }


@app.get("/api/documents/{doc_id}")
async def get_document(doc_id: str, request: Request):
    """Get a specific document by ID"""
    client_ip, request_id = get_client_info(request)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    doc = db.get_document(doc_id)
    if not doc:
        security_logger.log_security_event(
            event_type="document_not_found",
            severity="low",
            client_ip=client_ip,
            message=f"Attempted access to non-existent document: {doc_id}",
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Log document metadata access
    security_logger.log_document_access(
        client_ip=client_ip,
        document_id=doc_id,
        document_path=doc.get("path", ""),
        action="view_metadata",
        request_id=request_id,
        filename=doc.get("filename", "")
    )
    
    return doc


@app.get("/api/documents/{doc_id}/file")
async def get_document_file(doc_id: str, request: Request):
    """Get the actual document file for inline viewing"""
    client_ip, request_id = get_client_info(request)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    doc = db.get_document(doc_id)
    if not doc:
        security_logger.log_security_event(
            event_type="file_not_found",
            severity="low",
            client_ip=client_ip,
            message=f"Attempted file access for non-existent document: {doc_id}",
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=404, detail="Document not found")
    
    file_path = (BASE_PATH / doc["path"]).resolve()
    
    # Path traversal protection - ensure file is within BASE_PATH
    if not str(file_path).startswith(str(BASE_PATH.resolve())):
        security_logger.log_security_event(
            event_type="path_traversal_attempt",
            severity="high",
            client_ip=client_ip,
            message=f"Path traversal attempt detected: {doc['path']}",
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not file_path.exists():
        security_logger.log_security_event(
            event_type="file_missing",
            severity="medium",
            client_ip=client_ip,
            message=f"Document exists but file missing: {doc_id}",
            request_id=request_id,
            document_id=doc_id,
            expected_path=str(file_path)
        )
        raise HTTPException(status_code=404, detail="File not found")
    
    # Determine media type based on file extension
    ext = file_path.suffix.lower()
    media_types = {
        '.pdf': 'application/pdf',
        '.wav': 'audio/wav',
        '.mp3': 'audio/mpeg',
        '.mp4': 'video/mp4',
        '.mov': 'video/quicktime',
        '.m4a': 'audio/mp4',
    }
    media_type = media_types.get(ext, 'application/octet-stream')
    
    # Log file access (important for audit trail)
    file_size = file_path.stat().st_size
    security_logger.log_document_access(
        client_ip=client_ip,
        document_id=doc_id,
        document_path=doc["path"],
        action="download",
        request_id=request_id,
        filename=doc.get("filename", ""),
        file_type=ext,
        file_size_bytes=file_size
    )
    
    # Return file for inline viewing using FileResponse
    # FileResponse supports HTTP Range requests which are required for video/audio seeking
    from starlette.responses import FileResponse
    
    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={
            'Content-Disposition': 'inline',  # Forces inline display, not download
            'Accept-Ranges': 'bytes',  # Explicitly indicate we support range requests
        }
    )


@app.post("/api/ask")
async def ask_question(ask_request: AskRequest, request: Request):
    """Ask a question and get an AI-powered answer"""
    client_ip, request_id = get_client_info(request)
    
    if not llm or not llm.is_available():
        raise HTTPException(
            status_code=503, 
            detail="LLM not configured. Set OPENAI_API_KEY environment variable."
        )
    
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    
    # Get relevant documents
    context_docs = vector_store.search(
        query=ask_request.question,
        n_results=ask_request.num_context_docs,
        category=ask_request.category
    )
    
    # Log the LLM query
    security_logger.log_llm_query(
        client_ip=client_ip,
        question=ask_request.question,
        context_docs_count=len(context_docs),
        request_id=request_id,
        category=ask_request.category
    )
    
    if not context_docs:
        return {
            "question": ask_request.question,
            "answer": "No relevant documents found for this question.",
            "sources": []
        }
    
    # Get answer from LLM
    try:
        answer = llm.answer_question(ask_request.question, context_docs)
    except Exception as e:
        security_logger.log_error(
            error=e,
            context="llm_answer",
            client_ip=client_ip,
            request_id=request_id,
            question=ask_request.question[:100]
        )
        raise HTTPException(status_code=500, detail="Error generating answer")
    
    # Format sources with document IDs for linking
    sources = [
        {
            "id": doc.get("id"),
            "filename": doc.get("filename", "Unknown"),
            "category": doc.get("category", "Unknown"),
            "score": doc.get("score", 0)
        }
        for doc in context_docs
    ]
    
    return {
        "question": ask_request.question,
        "answer": answer,
        "sources": sources
    }


@app.post("/api/ask/stream")
async def ask_question_stream(ask_request: AskRequest, request: Request):
    """Ask a question and stream the response"""
    client_ip, request_id = get_client_info(request)
    
    if not llm or not llm.is_available():
        raise HTTPException(status_code=503, detail="LLM not configured")
    
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    
    # Get relevant documents
    context_docs = vector_store.search(
        query=ask_request.question,
        n_results=ask_request.num_context_docs,
        category=ask_request.category
    )
    
    # Log the streaming LLM query
    security_logger.log_llm_query(
        client_ip=client_ip,
        question=ask_request.question,
        context_docs_count=len(context_docs),
        request_id=request_id,
        category=ask_request.category,
        streaming=True
    )
    
    async def generate():
        for chunk in llm.answer_question(ask_request.question, context_docs, stream=True):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/documents/{doc_id}/summary")
async def get_document_summary(doc_id: str, request: Request, regenerate: bool = False):
    """Get an AI-generated summary of a document (cached if available)"""
    client_ip, request_id = get_client_info(request)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    doc = db.get_document(doc_id)
    if not doc:
        security_logger.log_security_event(
            event_type="summary_not_found",
            severity="low",
            client_ip=client_ip,
            message=f"Summary requested for non-existent document: {doc_id}",
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Check for cached summary first (unless regenerate is requested)
    if not regenerate:
        cached = db.get_summary(doc_id)
        if cached:
            security_logger.log_document_access(
                client_ip=client_ip,
                document_id=doc_id,
                document_path=doc.get("path", ""),
                action="summary_cached",
                request_id=request_id,
                filename=doc.get("filename", "")
            )
            return {
                "document_id": doc_id,
                "filename": doc["filename"],
                "summary": cached["summary"],
                "cached": True,
                "generated_at": cached["created_at"]
            }
    
    # No cached summary (or regenerate requested) - need to generate one
    if not llm or not llm.is_available():
        raise HTTPException(status_code=503, detail="LLM not configured")
    
    # Log document summary generation request
    security_logger.log_document_access(
        client_ip=client_ip,
        document_id=doc_id,
        document_path=doc.get("path", ""),
        action="summary_generate",
        request_id=request_id,
        filename=doc.get("filename", "")
    )
    
    try:
        summary = llm.summarize_document(doc)
        
        # Save the generated summary to cache
        db.save_summary(doc_id, summary)
        
        security_logger.log_system_event(
            "summary_cached",
            f"New summary cached for document: {doc_id}",
            document_id=doc_id
        )
    except Exception as e:
        security_logger.log_error(
            error=e,
            context="document_summary",
            client_ip=client_ip,
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=500, detail="Error generating summary")
    
    return {
        "document_id": doc_id,
        "filename": doc["filename"],
        "summary": summary,
        "cached": False
    }


# Mount static files last (if frontend exists)
if STATIC_PATH.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_PATH)), name="static")


if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    
    print(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


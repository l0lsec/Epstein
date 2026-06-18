"""
Epstein Files Search Platform - API Server
FastAPI backend for document search and LLM-powered analysis
"""

import os
import json
import asyncio
import uuid
import time as _time
import threading as _threading
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures as _cf
import sqlite3
from html import escape as html_escape
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, Response, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from io import BytesIO
from sse_starlette.sse import EventSourceResponse

from database import Database, VectorStore, build_index
from llm import LLMAssistant
from extractor import extract_email_date
from security_logger import (
    SecurityLogger, 
    RequestLoggingMiddleware, 
    get_security_logger,
    get_client_info
)
import httpx


# IP Geolocation cache and helper
_ip_geo_cache = {}

# Response cache for expensive endpoints (stats, categories)
# Simple time-based cache to reduce database load under heavy traffic
class ResponseCache:
    def __init__(self, ttl_seconds: int = 60):
        self._cache = {}
        self._ttl = ttl_seconds
    
    def get(self, key: str):
        if key in self._cache:
            data, timestamp = self._cache[key]
            if datetime.now().timestamp() - timestamp < self._ttl:
                return data
            del self._cache[key]
        return None
    
    def get_stale(self, key: str):
        """Return cached data even if expired (for stale-while-revalidate)."""
        if key in self._cache:
            data, _ = self._cache[key]
            return data
        return None
    
    def set(self, key: str, data):
        self._cache[key] = (data, datetime.now().timestamp())
    
    def invalidate(self, key: str = None):
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()

# Cache instances with different TTLs
_stats_cache = ResponseCache(ttl_seconds=3600)  # Stats cached for 1h; admin actions call invalidate() on change
_categories_cache = ResponseCache(ttl_seconds=3600)  # Categories cached for 1h; admin actions call invalidate() on change
_bootstrap_cache = ResponseCache(ttl_seconds=86400)  # Bootstrap cached for 24h; admin actions invalidate on change
_maintenance_cache = ResponseCache(ttl_seconds=120)
# Heavy admin-dashboard aggregations cached briefly so the auto-refreshing admin console
# can't re-fire ~10 expensive queries every few seconds and starve the thread pool.
_admin_cache = ResponseCache(ttl_seconds=30)
# Wall-clock ceiling (seconds) for heavy admin read queries so a runaway one aborts and
# releases its thread-pool worker instead of hanging for minutes.
_ADMIN_QUERY_TIMEOUT = 20
_bootstrap_refreshing = False  # Guard to prevent concurrent background rebuilds

_categories_lock = asyncio.Lock()
_subcategories_lock = asyncio.Lock()
_stats_lock = asyncio.Lock()

async def lookup_ip_geo(ip: str) -> dict:
    """Look up geolocation for an IP address using ip-api.com"""
    # Check cache first
    if ip in _ip_geo_cache:
        return _ip_geo_cache[ip]
    
    # Skip private/local IPs
    if (ip == 'unknown' or ip.startswith('127.') or ip.startswith('192.168.') or 
        ip.startswith('10.') or ip.startswith('172.16.') or ip == 'localhost' or
        ip.startswith('::1') or ip == ''):
        result = {'city': 'Local', 'country': 'Local', 'isp': 'Private Network', 'region': ''}
        _ip_geo_cache[ip] = result
        return result
    
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,regionName,city,isp,org"}
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    result = {
                        'city': data.get('city', 'Unknown'),
                        'region': data.get('regionName', ''),
                        'country': data.get('country', 'Unknown'),
                        'isp': data.get('isp') or data.get('org') or 'Unknown'
                    }
                    _ip_geo_cache[ip] = result
                    return result
    except Exception as e:
        pass  # Silently fail for geo lookups
    
    fallback = {'city': 'Unknown', 'country': 'Unknown', 'isp': 'Unknown', 'region': ''}
    _ip_geo_cache[ip] = fallback
    return fallback


async def enrich_with_geo(items: list, ip_field: str = 'client_ip', limit: int = 20) -> list:
    """Add geolocation data to a list of items with IP addresses"""
    # Get unique IPs
    unique_ips = list(set(item.get(ip_field, '') for item in items[:limit] if item.get(ip_field)))[:limit]
    
    # Batch lookup (with small delays to avoid rate limiting)
    geo_data = {}
    for ip in unique_ips:
        geo_data[ip] = await lookup_ip_geo(ip)
        await asyncio.sleep(0.05)  # Small delay to avoid rate limits
    
    # Enrich items
    for item in items:
        ip = item.get(ip_field, '')
        if ip in geo_data:
            geo = geo_data[ip]
            item['geo_city'] = geo.get('city', 'Unknown')
            item['geo_region'] = geo.get('region', '')
            item['geo_country'] = geo.get('country', 'Unknown')
            item['geo_isp'] = geo.get('isp', 'Unknown')
            # Format location string
            if geo['city'] == 'Unknown':
                item['geo_location'] = geo['country']
            elif geo['region']:
                item['geo_location'] = f"{geo['city']}, {geo['region']}, {geo['country']}"
            else:
                item['geo_location'] = f"{geo['city']}, {geo['country']}"
        else:
            item['geo_location'] = 'Unknown'
            item['geo_isp'] = 'Unknown'
    
    return items


# Configuration
BASE_PATH = Path(os.getenv("EPSTEIN_BASE_PATH", Path(__file__).parent.parent))
DB_PATH = BASE_PATH / "epstein.db"
VECTOR_PATH = BASE_PATH / "vector_store"
STATIC_PATH = BASE_PATH / "frontend"
THUMBNAILS_PATH = BASE_PATH / "thumbnails"
MAINTENANCE_LOCK = BASE_PATH / ".maintenance"

# Thumbnail settings
THUMBNAIL_WIDTH = 200
THUMBNAIL_HEIGHT = 280  # Approximate A4 aspect ratio

# Auto-indexing configuration (in seconds)
# NOTE: Auto-indexing is disabled by default. Use admin dashboard to trigger reindex manually.
AUTO_INDEX_INTERVAL = int(os.getenv("AUTO_INDEX_INTERVAL", "172800"))  # Default: 48 hours (if enabled)
AUTO_INDEX_ENABLED = os.getenv("AUTO_INDEX_ENABLED", "false").lower() == "true"

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
        security_logger.set_database(db)  # enable telemetry dual-write
        doc_count = db.count_documents(include_hidden=True)
        security_logger.log_system_event(
            "database_loaded",
            f"Database loaded with {doc_count} documents",
            document_count=doc_count
        )
        
        # Auto-seed default keywords if none exist
        keywords_added = db.seed_default_keywords()
        if keywords_added > 0:
            security_logger.log_system_event(
                "keywords_seeded",
                f"Seeded {keywords_added} default keywords",
                keywords_added=keywords_added
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
    
    # Pre-warm the bootstrap cache so the first user request is instant.
    if db:
        import random
        stagger = random.uniform(0, 30.0)
        await asyncio.sleep(stagger)
        security_logger.log_system_event("cache_warmup", "Pre-warming bootstrap cache...")
        try:
            await asyncio.to_thread(_build_and_cache_bootstrap)
            security_logger.log_system_event("cache_warmup_done", "Bootstrap cache ready")
        except Exception as e:
            security_logger.log_system_event(
                "cache_warmup_failed",
                f"Bootstrap warmup failed: {e}",
                severity="warning"
            )
    
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
    lifespan=lifespan,
    docs_url=None,  # Disable default docs - we'll add protected versions
    redoc_url=None  # Disable default redoc
)

# CORS for frontend - configurable origins
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Security logging middleware - logs all requests with timing and security checks
app.add_middleware(RequestLoggingMiddleware, security_logger=security_logger)


# Maintenance mode middleware - serves maintenance page when .maintenance file exists
# or when custom status page is enabled via admin panel
@app.middleware("http")
async def maintenance_check(request: Request, call_next):
    """Check if site is in maintenance mode and serve maintenance page if so"""
    path = request.url.path
    
    # Always allow these endpoints through (needed for maintenance page to work)
    allowed_paths = (
        "/api/health",
        "/api/maintenance-status",  # Progress API for maintenance page (legacy)
        "/api/maintenance-stream",  # SSE stream for maintenance page
        "/api/admin/",  # Admin API endpoints (for managing status page)
        "/admin",       # Admin console page
        "/static/favicon",
        "/static/favicon.svg",
        "/static/admin",  # Admin panel assets
    )
    if any(path.startswith(p) or path == p for p in allowed_paths):
        return await call_next(request)
    
    # Check for maintenance mode - either .maintenance file OR custom status page enabled
    show_maintenance = False
    
    # First check: .maintenance file exists (indexing in progress)
    if MAINTENANCE_LOCK.exists():
        show_maintenance = True
    elif db:
        status_enabled = _maintenance_cache.get("status_page_enabled")
        if status_enabled is None:
            status_enabled = await asyncio.to_thread(db.get_setting, "status_page_enabled", "false")
            _maintenance_cache.set("status_page_enabled", status_enabled)
        if status_enabled == "true":
            show_maintenance = True
    
    if show_maintenance:
        maintenance_page = STATIC_PATH / "maintenance.html"
        if maintenance_page.exists():
            return FileResponse(
                maintenance_page,
                media_type="text/html",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
            )
    
    return await call_next(request)


# Security headers and caching middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    
    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    # Allow framing for document file endpoints (PDF viewer uses iframe)
    # Block framing for all other pages to prevent clickjacking
    if path.endswith("/file") and "/api/documents/" in path:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    
    # Cache headers for static files (enables Cloudflare caching)
    if path.startswith("/static/"):
        # Long cache for versioned assets and fonts/images
        if "?v=" in str(request.url) or path.endswith(('.svg', '.png', '.ico', '.woff2', '.woff', '.jpg', '.jpeg', '.gif')):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # 1 year
        # Shorter cache for HTML
        elif path.endswith(('.html', '.htm')):
            response.headers["Cache-Control"] = "public, max-age=300"  # 5 minutes
        # Medium cache for CSS/JS
        else:
            response.headers["Cache-Control"] = "public, max-age=86400"  # 24 hours
    
    # Aggressive caching for thumbnails (they never change once generated)
    elif "/thumbnail" in path and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=2592000, immutable"  # 30 days
    
    # Document files are immutable — let Cloudflare cache to cut origin egress
    elif path.endswith("/file") and "/api/documents/" in path and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=604800"  # 7 days
    
    # Short CDN cache for homepage (s-maxage for CF, shorter max-age for browsers)
    elif path == "/" and response.status_code == 200:
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "public, s-maxage=300, max-age=60"
    
    return response


# Admin API key for protected endpoints
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")

# Admin IP whitelist - only these IPs can access admin console (comma-separated)
# Set to empty to allow any IP with valid API key
ADMIN_IP_WHITELIST = set(filter(None, os.getenv("ADMIN_IP_WHITELIST", "").split(",")))

def verify_admin_access(request: Request, x_api_key: str = None) -> tuple[bool, str]:
    """
    Verify admin access by checking:
    1. API key must match ADMIN_API_KEY
    2. If ADMIN_IP_WHITELIST is set, client IP must be in whitelist
    Returns (is_authorized, error_message)
    """
    client_ip, request_id = get_client_info(request)
    
    # Check if admin key is configured
    if not ADMIN_API_KEY:
        return False, "Admin access not configured"
    
    # Check API key
    if not x_api_key or x_api_key != ADMIN_API_KEY:
        security_logger.log_security_event(
            event_type="unauthorized_admin_access",
            severity="high",
            client_ip=client_ip,
            message="Invalid or missing admin API key",
            request_id=request_id,
            endpoint=str(request.url.path)
        )
        return False, "Invalid API key"
    
    # Check IP whitelist if configured
    if ADMIN_IP_WHITELIST and client_ip not in ADMIN_IP_WHITELIST:
        security_logger.log_security_event(
            event_type="admin_ip_blocked",
            severity="high",
            client_ip=client_ip,
            message=f"Admin access denied: IP {client_ip} not in whitelist",
            request_id=request_id,
            endpoint=str(request.url.path)
        )
        return False, "IP not authorized"
    
    return True, ""


# Boolean Query Parser for FTS5
import re

def parse_boolean_query(query: str) -> dict:
    """
    Parse user-friendly Boolean search syntax and convert to FTS5 format.
    
    Supported syntax:
    - -term or -"phrase" → FTS5 NOT term (exclusion)
    - term1 term2 → term1 AND term2 (implicit AND between words)
    - term1 OR term2 → preserved (explicit OR)
    - term1 AND term2 → preserved (explicit AND)
    - "exact phrase" → preserved (phrase search)
    - term* → preserved (prefix matching)
    
    Returns:
        dict: {
            'fts_query': str,  # Converted FTS5 query
            'original_query': str,  # Original input
            'parsed_info': {
                'excluded_terms': [],  # Terms prefixed with -
                'required_terms': [],  # Regular terms (ANDed together)
                'phrases': [],  # Quoted phrases
                'has_or': bool,  # Whether OR operator is used
                'has_wildcards': bool  # Whether prefix wildcards are used
            }
        }
    """
    if not query or not query.strip():
        return {
            'fts_query': '',
            'original_query': query,
            'parsed_info': {
                'excluded_terms': [],
                'required_terms': [],
                'phrases': [],
                'has_or': False,
                'has_wildcards': False
            }
        }
    
    original_query = query.strip()
    
    # Track parsed components
    excluded_terms = []
    required_terms = []
    phrases = []
    has_or = False
    has_wildcards = False
    
    # First, extract all quoted phrases (both excluded and included)
    # Pattern matches: -"phrase" or "phrase"
    phrase_pattern = r'(-?)"([^"]+)"'
    
    def process_phrase(match):
        nonlocal excluded_terms, phrases
        is_excluded = match.group(1) == '-'
        phrase = match.group(2).strip()
        if phrase:
            if is_excluded:
                excluded_terms.append(f'"{phrase}"')
            else:
                phrases.append(phrase)
        # Return placeholder to preserve position
        return f' __PHRASE_{len(phrases) + len(excluded_terms) - 1}__ '
    
    # Store phrases and replace with placeholders
    phrase_map = {}
    phrase_counter = [0]
    
    def extract_phrase(match):
        nonlocal phrase_counter
        is_excluded = match.group(1) == '-'
        phrase = match.group(2).strip()
        if phrase:
            placeholder = f'__PHRASE{phrase_counter[0]}__'
            phrase_map[placeholder] = (is_excluded, phrase)
            phrase_counter[0] += 1
            if is_excluded:
                excluded_terms.append(f'"{phrase}"')
            else:
                phrases.append(phrase)
            return f' {placeholder} '
        return ''
    
    working_query = re.sub(phrase_pattern, extract_phrase, original_query)
    
    # Check for OR operator (case insensitive)
    if re.search(r'\bOR\b', working_query, re.IGNORECASE):
        has_or = True
    
    # Check for wildcards
    if '*' in working_query:
        has_wildcards = True
    
    # Tokenize remaining query (split on whitespace)
    tokens = working_query.split()
    
    # Process tokens
    processed_tokens = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        # Skip empty tokens
        if not token.strip():
            i += 1
            continue
        
        # Handle phrase placeholders
        if token.startswith('__PHRASE') and token.endswith('__'):
            if token in phrase_map:
                is_excluded, phrase = phrase_map[token]
                if is_excluded:
                    processed_tokens.append('NOT')
                    processed_tokens.append(f'"{phrase}"')
                else:
                    processed_tokens.append(f'"{phrase}"')
            i += 1
            continue
        
        # Handle excluded terms (-term)
        if token.startswith('-') and len(token) > 1:
            term = token[1:]
            # Clean the term of any special chars that might break FTS5
            term = re.sub(r'[^\w*]', '', term)
            if term:
                excluded_terms.append(term)
                processed_tokens.append('NOT')
                processed_tokens.append(term)
            i += 1
            continue
        
        # Handle OR (preserve as-is, case insensitive -> uppercase)
        if token.upper() == 'OR':
            processed_tokens.append('OR')
            i += 1
            continue
        
        # Handle AND (preserve as-is, case insensitive -> uppercase)
        if token.upper() == 'AND':
            processed_tokens.append('AND')
            i += 1
            continue
        
        # Handle NOT (preserve as-is for explicit NOT usage)
        if token.upper() == 'NOT':
            processed_tokens.append('NOT')
            i += 1
            continue
        
        # Regular term - clean it and add
        # Allow alphanumeric, underscore, and wildcard
        term = re.sub(r'[^\w*]', '', token)
        if term:
            required_terms.append(term.rstrip('*'))  # Store without wildcard for display
            processed_tokens.append(term)
        
        i += 1
    
    # Build FTS5 query
    # Insert AND between adjacent terms that aren't already connected by operators
    fts_tokens = []
    for i, token in enumerate(processed_tokens):
        fts_tokens.append(token)
        
        # Check if we need to insert AND before the next token
        if i < len(processed_tokens) - 1:
            current_upper = token.upper()
            next_upper = processed_tokens[i + 1].upper()
            
            # Don't add AND if current or next token is already an operator
            if current_upper not in ['AND', 'OR', 'NOT'] and next_upper not in ['AND', 'OR', 'NOT']:
                # Don't add AND if current token is NOT (NOT should connect to next)
                if not current_upper.startswith('NOT '):
                    fts_tokens.append('AND')
    
    fts_query = ' '.join(fts_tokens)
    
    # Clean up any double spaces
    fts_query = re.sub(r'\s+', ' ', fts_query).strip()
    
    # Handle edge case: query is only exclusions (e.g., "-Maxwell") - FTS5 can't handle NOT without a positive term
    if not required_terms and not phrases and excluded_terms:
        # Exclusion-only query - return empty fts_query so the caller can handle gracefully
        fts_query = ''
    
    return {
        'fts_query': fts_query,
        'original_query': original_query,
        'parsed_info': {
            'excluded_terms': excluded_terms,
            'required_terms': required_terms,
            'phrases': phrases,
            'has_or': has_or,
            'has_wildcards': has_wildcards
        }
    }


# Request/Response Models
class SearchRequest(BaseModel):
    query: str
    search_type: str = "hybrid"  # "fulltext", "semantic", "hybrid"
    category: Optional[str] = None
    subcategory: Optional[str] = None
    file_type: Optional[str] = None  # "pdf", "document", "image", "audio", "video"
    date_from: Optional[str] = None  # YYYY-MM-DD format
    date_to: Optional[str] = None    # YYYY-MM-DD format
    limit: int = 50  # Results per page (unlimited total via pagination)
    offset: int = 0


class AskRequest(BaseModel):
    question: str
    category: Optional[str] = None
    num_context_docs: int = 8  # Increased default for better accuracy


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
    email: str
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
async def root(doc: str = None):
    """Serve the frontend with dynamic OG tags for document shares"""
    index_path = STATIC_PATH / "index.html"
    if not index_path.exists():
        return {"message": "Epstein Files Search Platform API", "docs": "/docs"}
    
    # If doc param present, serve modified HTML with document's thumbnail for social sharing
    if doc and db:
        document = await asyncio.to_thread(db.get_document, doc)
        if document:
            try:
                html = index_path.read_text()
                # Escape user-controlled values to prevent XSS via OG tag injection
                doc_escaped = html_escape(doc, quote=True)
                filename_raw = document.get("filename", "Document")
                filename_escaped = html_escape(filename_raw, quote=True)
                thumbnail_url = f"https://epsteinfta.com/api/documents/{doc_escaped}/thumbnail"
                
                # Replace og:image tags with document thumbnail
                html = html.replace(
                    'content="https://epsteinfta.com/static/og-image.png"',
                    f'content="{thumbnail_url}"'
                )
                # Replace og:title with document name
                html = html.replace(
                    'content="Epstein Files Public Archive | Public Document Search"',
                    f'content="{filename_escaped} | Epstein Files Public Archive"'
                )
                # Replace twitter:title as well
                html = html.replace(
                    'content="Epstein Files Public Archive | Public Document Search">',
                    f'content="{filename_escaped} | Epstein Files Public Archive">'
                )
                
                return HTMLResponse(content=html)
            except Exception:
                # Fall through to default on any error
                pass
    
    return FileResponse(index_path)


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


@app.api_route("/ads.txt", methods=["GET", "HEAD"])
async def ads_txt():
    """Serve ads.txt for ad-network (Ezoic) authorization.

    Ezoic's "Ads.txt Manager" approach is preferred: set ADS_TXT_REDIRECT_URL
    to your managed URL (e.g. https://srv.adstxtmanager.com/XXXXX/epsteinfta.com)
    and we 301 to it so Ezoic can keep the authorized-seller list current.
    Falls back to a static frontend/ads.txt for the manual line-list approach.
    """
    redirect_url = os.getenv("ADS_TXT_REDIRECT_URL", "").strip()
    if redirect_url:
        return RedirectResponse(url=redirect_url, status_code=301)
    ads_path = STATIC_PATH / "ads.txt"
    if ads_path.exists():
        return FileResponse(ads_path, media_type="text/plain")
    return Response(status_code=404)


def get_maintenance_status_data() -> dict:
    """Helper function to get current maintenance status data.
    
    Used by both the REST endpoint and SSE stream.
    Returns a dict with maintenance status information.
    """
    # Check for .maintenance file first (indexing mode takes priority)
    if MAINTENANCE_LOCK.exists():
        try:
            data = json.loads(MAINTENANCE_LOCK.read_text())
            return {
                "active": True,
                "mode": "indexing",
                "started": data.get("started"),
                "step": data.get("step", 0),
                "step_name": data.get("step_name", "Initializing..."),
                "current": data.get("current", 0),
                "total": data.get("total", 0),
                "percent": data.get("percent", 0),
                "message": data.get("message", "Processing...")
            }
        except Exception:
            return {"active": True, "mode": "indexing", "step": 0, "step_name": "Processing...", "percent": 0}
    
    # Check for custom status page mode
    if db:
        status_enabled = db.get_setting("status_page_enabled", "false")
        if status_enabled == "true":
            return {
                "active": True,
                "mode": "maintenance",
                "title": db.get_setting("status_page_title", "Under Maintenance"),
                "message": db.get_setting("status_page_message", "We're performing scheduled maintenance. Please check back soon."),
                "timeline": db.get_setting("status_page_timeline", ""),
                "started": db.get_setting("status_page_started", "")
            }
    
    return {"active": False}


@app.get("/api/maintenance-status")
async def get_maintenance_status():
    """Get current maintenance mode status and progress
    
    Returns progress information when site is in maintenance mode.
    Supports two modes:
    - "indexing": Shows progress when .maintenance file exists (document indexing)
    - "maintenance": Shows custom message when status_page_enabled is true
    
    Used by maintenance.html to show appropriate content to users.
    """
    return await asyncio.to_thread(get_maintenance_status_data)


@app.get("/api/maintenance-stream")
async def maintenance_stream(request: Request):
    """SSE stream for maintenance status updates.
    
    Provides real-time updates for the maintenance page without constant polling.
    - Sends status immediately on connection
    - For indexing mode: sends updates every 5 seconds when progress changes
    - For maintenance mode: sends heartbeat every 30 seconds
    - Automatically closes and signals reload when maintenance ends
    """
    async def event_generator():
        last_status_json = None
        
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break
            
            current_status = await asyncio.to_thread(get_maintenance_status_data)
            current_status_json = json.dumps(current_status, sort_keys=True)
            
            # Send update if status changed or first connection
            if current_status_json != last_status_json:
                yield {
                    "event": "status",
                    "data": json.dumps(current_status)
                }
                last_status_json = current_status_json
                
                # If maintenance is off, send final message and close
                if not current_status.get("active"):
                    yield {
                        "event": "online",
                        "data": "{}"
                    }
                    break
            
            # For indexing mode, check more frequently for progress updates
            # For maintenance mode, just heartbeat less frequently
            wait_time = 5 if current_status.get("mode") == "indexing" else 30
            await asyncio.sleep(wait_time)
    
    return EventSourceResponse(event_generator())


@app.get("/api/stats")
async def get_stats(response: Response) -> StatsResponse:
    """Get platform statistics (cached for 1h)"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    cached = _stats_cache.get("stats")
    if cached:
        response.headers["X-Cache"] = "HIT"
        response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
        return cached

    async with _stats_lock:
        cached = _stats_cache.get("stats")
        if cached:
            response.headers["X-Cache"] = "HIT"
            response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
            return cached

        stats = await asyncio.to_thread(db.get_stats)
        result = StatsResponse(
            total_documents=stats["total_documents"],
            total_pages=stats["total_pages"],
            by_category=stats["by_category"],
            by_subcategory=stats["by_subcategory"],
            by_file_type=stats.get("by_file_type", []),
            vector_chunks=vector_store.get_count() if vector_store else 0,
            llm_available=llm.is_available() if llm else False
        )

        _stats_cache.set("stats", result)
        response.headers["X-Cache"] = "MISS"
        response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
        return result


def _build_and_cache_bootstrap() -> dict:
    """Build the full bootstrap payload and store it in _bootstrap_cache.
    
    Callable from startup warmup (in a thread) or from the request handler.
    """
    stats = db.get_stats()
    stats_response = StatsResponse(
        total_documents=stats["total_documents"],
        total_pages=stats["total_pages"],
        by_category=stats["by_category"],
        by_subcategory=stats["by_subcategory"],
        by_file_type=stats.get("by_file_type", []),
        vector_chunks=vector_store.get_count() if vector_store else 0,
        llm_available=llm.is_available() if llm else False,
    )
    categories = db.get_category_counts(keyword=None, include_hidden=False)
    keywords_list = db.get_keywords(active_only=True)
    grouped_keywords = {}
    for kw in keywords_list:
        cat = kw["category"]
        if cat not in grouped_keywords:
            grouped_keywords[cat] = []
        grouped_keywords[cat].append({
            "name": kw["name"],
            "search_term": kw["search_term"],
            "document_count": kw["document_count"],
        })
    settings = db.get_all_settings()
    public_settings = {
        "ask_ai_enabled": settings.get("ask_ai_enabled", "true") == "true",
        "pinned_documents_enabled": settings.get("pinned_documents_enabled", "true") == "true",
        # Default OFF for ads: only flips on after an ad network is wired up in app.js AD_CONFIG.
        "ads_enabled": settings.get("ads_enabled", "false") == "true",
        # Default ON: the Amazon Associates strip stays hidden anyway until a tag is configured in app.js.
        "affiliate_enabled": settings.get("affiliate_enabled", "true") == "true",
    }

    browse_limit = 24
    browse_docs, browse_total = db.get_all_documents_with_total(
        limit=browse_limit, offset=0, include_hidden=False
    )
    _categories_cache.set("docs_total_unfiltered", browse_total)

    pinned_docs = []
    if public_settings.get("pinned_documents_enabled", True):
        pinned_docs = db.get_pinned_documents(include_hidden=False)

    result = {
        "stats": stats_response,
        "categories": {"categories": categories},
        "keywords": {"keywords": grouped_keywords},
        "settings": public_settings,
        "browse": {
            "total": browse_total,
            "limit": browse_limit,
            "offset": 0,
            "documents": browse_docs,
        },
        "pinned_documents": pinned_docs,
    }
    _bootstrap_cache.set("bootstrap", result)
    return result


async def _background_refresh_bootstrap():
    """Rebuild the bootstrap cache in the background (fire-and-forget)."""
    global _bootstrap_refreshing
    if _bootstrap_refreshing:
        return
    _bootstrap_refreshing = True
    try:
        await asyncio.to_thread(_build_and_cache_bootstrap)
    except Exception:
        pass
    finally:
        _bootstrap_refreshing = False


@app.get("/api/bootstrap")
async def get_bootstrap(response: Response):
    """Single request for initial page load: stats, categories, keywords, and public settings.
    Reduces 4 round-trips to 1 for faster first paint."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    cached = _bootstrap_cache.get("bootstrap")
    if cached:
        response.headers["X-Cache"] = "HIT"
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
        return cached
    
    stale = _bootstrap_cache.get_stale("bootstrap")
    if stale:
        asyncio.create_task(_background_refresh_bootstrap())
        response.headers["X-Cache"] = "STALE"
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
        return stale
    
    result = await asyncio.to_thread(_build_and_cache_bootstrap)
    response.headers["X-Cache"] = "MISS"
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    return result


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
    """Manually trigger a re-index (requires admin authentication)"""
    global db, vector_store, last_index_time, is_indexing
    
    # Verify admin access
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    client_ip, request_id = get_client_info(request)
    
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

        def _work():
            return db.get_stats() if db else {}

        stats = await asyncio.to_thread(_work)

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
    """Rebuild the FTS5 full-text search index to fix sync issues (requires admin authentication)"""
    global db
    
    # Verify admin access
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    client_ip, request_id = get_client_info(request)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    try:
        security_logger.log_system_event(
            "fts_rebuild_start",
            f"FTS index rebuild triggered from {client_ip}"
        )
        
        await asyncio.to_thread(db.rebuild_fts)
        
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
async def get_categories(keyword: Optional[str] = None, response: Response = None):
    """Get all document categories, optionally filtered by keyword (cached for 60 seconds)

    Note: Hidden categories and documents in hidden categories are excluded from results.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    cache_key = f"categories:{keyword or 'all'}"
    if not keyword:
        cached = _categories_cache.get(cache_key)
        if cached:
            if response:
                response.headers["X-Cache"] = "HIT"
                response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
            return cached

    async with _categories_lock:
        if not keyword:
            cached = _categories_cache.get(cache_key)
            if cached:
                if response:
                    response.headers["X-Cache"] = "HIT"
                    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
                return cached

        categories = await asyncio.to_thread(db.get_category_counts, keyword=keyword, include_hidden=False)
        result = {"categories": categories}

        if not keyword:
            _categories_cache.set(cache_key, result)

        if response:
            response.headers["X-Cache"] = "MISS" if not keyword else "BYPASS"
            response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
        return result


_subcategories_cache = ResponseCache(ttl_seconds=3600)  # 1h; admin actions invalidate via _categories_cache pattern

@app.get("/api/subcategories")
async def get_subcategories(category: Optional[str] = None):
    """Get subcategories, optionally filtered by category

    Note: Returns empty list if the category is hidden.
    Uses a dedicated lightweight query instead of full stats.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if category and await asyncio.to_thread(db.is_category_hidden, category):
        return {"subcategories": []}

    cache_key = f"subcategories:{category or 'all'}"
    cached = _subcategories_cache.get(cache_key)
    if cached:
        return cached

    async with _subcategories_lock:
        cached = _subcategories_cache.get(cache_key)
        if cached:
            return cached

        subcategories = await asyncio.to_thread(db.get_subcategory_counts, category=category, include_hidden=False)
        subcategories = [{"subcategory": s["subcategory"], "count": s["count"]} for s in subcategories if s.get("subcategory")]

        result = {"subcategories": subcategories}
        _subcategories_cache.set(cache_key, result)
        return result


@app.post("/api/feedback")
async def submit_feedback(feedback: FeedbackRequest, request: Request):
    """Submit user feedback with spam protection"""
    import httpx
    
    # Get client info for logging
    client_ip, request_id = get_client_info(request)
    
    # Verify reCAPTCHA (skip if secret key not configured or token missing due to client-side blocker)
    if RECAPTCHA_SECRET_KEY:
        if not feedback.recaptcha_token:
            security_logger.log_security_event(
                event_type="recaptcha_skipped",
                severity="low",
                client_ip=client_ip,
                message="reCAPTCHA token missing (likely blocked by client extension)",
                request_id=request_id
            )
        else:
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

    stripped_query = search_request.query.strip()
    if len(stripped_query) < 2:
        raise HTTPException(status_code=400, detail="Search query must be at least 2 characters")
    
    results = []
    total_count = 0
    
    # Parse the query for Boolean operators (for full-text search)
    parsed = parse_boolean_query(search_request.query)
    fts_query = parsed['fts_query']
    
    def _do_search():
        """Run all search DB work in a thread to avoid blocking the event loop."""
        _results = []
        _total = 0
        _facets = {}
        _error = None
        _timed_out = False

        if search_request.search_type in ["fulltext", "hybrid"]:
            try:
                ft_results = db.search_fulltext(
                    query=fts_query, limit=search_request.limit,
                    offset=search_request.offset, category=search_request.category,
                    subcategory=search_request.subcategory, file_type=search_request.file_type,
                    date_from=search_request.date_from, date_to=search_request.date_to,
                    include_hidden=False
                )
                for r in ft_results:
                    _results.append({**r, "search_type": "fulltext", "score": abs(r.get("score", 0))})
            except sqlite3.OperationalError:
                _timed_out = True
            except Exception as e:
                _error = e
                if search_request.search_type == "fulltext":
                    return _results, _total, _facets, _error

            if not _timed_out and _results:
                try:
                    _total = db.count_fulltext_results(
                        query=fts_query, category=search_request.category,
                        subcategory=search_request.subcategory, file_type=search_request.file_type,
                        date_from=search_request.date_from, date_to=search_request.date_to,
                        include_hidden=False
                    )
                except sqlite3.OperationalError:
                    _total = len(_results) + search_request.limit
            elif _timed_out:
                _total = 0

        if search_request.search_type in ["semantic", "hybrid"] and vector_store:
            sem_results = vector_store.search(
                query=search_request.query, n_results=search_request.limit,
                category=search_request.category
            )
            existing_ids = {r["id"] for r in _results}
            for r in sem_results:
                doc_id = r.get("id", "")
                if doc_id and doc_id not in existing_ids:
                    if not db.is_document_visible(doc_id):
                        continue
                    doc = db.get_document(doc_id, include_full_text=False)
                    if doc:
                        text = r.get("text", "")
                        _results.append({
                            "id": doc_id,
                            "filename": doc.get("filename", r.get("filename", "Unknown")),
                            "path": doc.get("path", r.get("path", "")),
                            "category": doc.get("category", r.get("category", "Unknown")),
                            "subcategory": doc.get("subcategory", r.get("subcategory", "")),
                            "file_type": doc.get("file_type", "pdf"),
                            "page_count": doc.get("page_count"),
                            "duration_seconds": doc.get("duration_seconds"),
                            "snippet": text[:300] + "..." if len(text) > 300 else text,
                            "search_type": "semantic", "score": r.get("score", 0)
                        })
                        existing_ids.add(doc_id)
            if search_request.search_type == "semantic":
                _total = len(_results)

        _results.sort(key=lambda x: x.get("score", 0), reverse=True)

        if not _timed_out and search_request.search_type in ["fulltext", "hybrid"]:
            try:
                _facets = db.get_search_facets(
                    query=fts_query, category=search_request.category,
                    subcategory=search_request.subcategory, file_type=search_request.file_type,
                    date_from=search_request.date_from, date_to=search_request.date_to,
                    include_hidden=False
                )
            except (sqlite3.OperationalError, Exception):
                pass

        return _results, _total, _facets, _error

    results, total_count, facets, search_error = await asyncio.to_thread(_do_search)

    if search_error:
        security_logger.log_error(
            error=search_error, context="fulltext_search",
            client_ip=client_ip, request_id=request_id,
            query=search_request.query[:100]
        )
        if search_request.search_type == "fulltext":
            raise HTTPException(status_code=400, detail="Search query error. Please check your search syntax.")
    
    # Log the search query for audit
    security_logger.log_search_query(
        client_ip=client_ip,
        query=search_request.query,
        search_type=search_request.search_type,
        result_count=total_count,
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
        "facets": facets,
        "parsed_query": {
            "fts_query": parsed['fts_query'],
            "excluded_terms": parsed['parsed_info']['excluded_terms'],
            "required_terms": parsed['parsed_info']['required_terms'],
            "phrases": parsed['parsed_info']['phrases'],
            "has_or": parsed['parsed_info']['has_or'],
            "has_wildcards": parsed['parsed_info']['has_wildcards']
        }
    }


@app.get("/api/documents")
async def list_documents(
    response: Response,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    file_type: Optional[str] = None,
    filename: Optional[str] = None,
    keyword: Optional[str] = None,
    search: Optional[str] = None
):
    """List all documents with pagination (cached for common queries)
    
    Args:
        search: Searches both filename AND subcategory (for admin document search)
    
    Note: Hidden documents and hidden categories are excluded from results.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Create cache key for filtered queries
    cache_key = f"docs:{limit}:{offset}:{category}:{subcategory}:{file_type}:{keyword}"
    
    # Only cache simple queries (no filename search, no text search)
    can_cache = not filename and not search
    
    if can_cache:
        cached = _categories_cache.get(cache_key)  # Reuse categories cache (60s TTL)
        if cached:
            response.headers["X-Cache"] = "HIT"
            response.headers["Cache-Control"] = "public, max-age=30, s-maxage=30"
            return cached
    
    # Unfiltered browse: no category, subcategory, file_type, filename, keyword, search
    unfiltered = not category and not subcategory and not file_type and not filename and not keyword and not search
    
    def _do_browse():
        if unfiltered:
            cached_total = _categories_cache.get("docs_total_unfiltered")
            if cached_total is not None:
                _docs = db.get_all_documents(limit=limit, offset=offset, include_hidden=False)
                return _docs, cached_total
            _docs, _total = db.get_all_documents_with_total(limit=limit, offset=offset, include_hidden=False)
            _categories_cache.set("docs_total_unfiltered", _total)
            return _docs, _total
        elif not keyword and not search:
            return db.get_all_documents_with_total(
                limit=limit, offset=offset, category=category, subcategory=subcategory,
                file_type=file_type, filename=filename, include_hidden=False,
            )
        else:
            _docs = db.get_all_documents(limit=limit, offset=offset, category=category, subcategory=subcategory, file_type=file_type, filename=filename, keyword=keyword, search=search, include_hidden=False)
            _total = db.count_documents(category=category, subcategory=subcategory, file_type=file_type, filename=filename, keyword=keyword, include_hidden=False)
            return _docs, _total

    docs, total = await asyncio.to_thread(_do_browse)
    
    result = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "documents": docs
    }
    
    if can_cache:
        _categories_cache.set(cache_key, result)
        response.headers["X-Cache"] = "MISS"
    else:
        response.headers["X-Cache"] = "BYPASS"
    
    response.headers["Cache-Control"] = "public, max-age=30, s-maxage=30"
    return result


@app.get("/api/documents/export")
async def export_documents(
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    file_type: Optional[str] = None,
    filename: Optional[str] = None,
    keyword: Optional[str] = None,
    search_query: Optional[str] = None,
    search_type: str = "fulltext",
    include_text: bool = False
):
    """Export documents as a list for CSV download
    
    Returns all matching documents with metadata and DOJ direct link.
    When include_text=true, full_text is included but results are capped at 5,000.
    
    Args:
        category: Filter by category
        subcategory: Filter by subcategory
        file_type: Filter by file type
        filename: Partial filename match
        keyword: Keyword search
        search_query: Full-text search query
        search_type: Type of search (fulltext, semantic, hybrid)
        include_text: Include extracted text content (caps results at 5,000)
    
    Returns:
        JSON with documents array containing filename, category, subcategory,
        file_type, page_count, char_count, document_date, doj_url,
        and optionally full_text
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Parse search query for FTS if provided
    fts_query = None
    if search_query and search_type in ["fulltext", "hybrid"]:
        parsed = parse_boolean_query(search_query)
        fts_query = parsed['fts_query']
    
    try:
        documents = await asyncio.to_thread(
            db.get_documents_for_export,
            category=category, subcategory=subcategory,
            file_type=file_type, filename=filename,
            keyword=keyword, search_query=fts_query,
            include_text=include_text
        )
        
        return {
            "total": len(documents),
            "documents": documents
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@app.get("/api/documents/{doc_id}")
async def get_document(
    doc_id: str,
    request: Request,
    include_text: bool = Query(True, description="Include full_text in response (set false for faster modal open)"),
):
    """Get a specific document by ID
    
    Use include_text=false for faster loading when only metadata is needed (e.g. opening modal).
    Fetch full text via GET /api/documents/{doc_id}/text when user opens the Text Content tab.
    
    Note: Returns 404 for hidden documents or documents in hidden categories.
    """
    client_ip, request_id = get_client_info(request)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Check if document is visible (not hidden and category not hidden)
    doc = await asyncio.to_thread(db.get_document, doc_id, include_hidden=False, include_full_text=include_text)
    if not doc:
        security_logger.log_security_event(
            event_type="document_not_found",
            severity="low",
            client_ip=client_ip,
            message=f"Attempted access to non-existent or hidden document: {doc_id}",
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=404, detail="Document not found")
    
    security_logger.log_document_access(
        client_ip=client_ip,
        document_id=doc_id,
        document_path=doc.get("path", ""),
        action="view_metadata",
        request_id=request_id,
        filename=doc.get("filename", "")
    )
    
    return doc


@app.get("/api/documents/{doc_id}/text")
async def get_document_text(doc_id: str, request: Request):
    """Get only the full text of a document (for lazy loading in modal).
    
    Note: Returns 404 for hidden documents or documents in hidden categories.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    full_text = await asyncio.to_thread(db.get_document_full_text, doc_id, include_hidden=False)
    if full_text is None:
        raise HTTPException(status_code=404, detail="Document not found")
    
    return {"full_text": full_text}


def _serve_document_file(doc: dict, doc_id: str, client_ip: str, request_id: str,
                         admin: bool = False):
    """Resolve a document's on-disk file and return a FileResponse for inline
    viewing. Shared by the public file route and the admin (hidden-bypass) route.
    `doc` must already be fetched with the appropriate visibility check.
    """
    from starlette.responses import FileResponse

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
        # Image types
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.tif': 'image/tiff',
        '.tiff': 'image/tiff',
        '.bmp': 'image/bmp',
        '.webp': 'image/webp',
    }
    media_type = media_types.get(ext, 'application/octet-stream')

    # Log file access (important for audit trail)
    file_size = file_path.stat().st_size
    security_logger.log_document_access(
        client_ip=client_ip,
        document_id=doc_id,
        document_path=doc["path"],
        action="admin_download" if admin else "download",
        request_id=request_id,
        filename=doc.get("filename", ""),
        file_type=ext,
        file_size_bytes=file_size
    )

    # FileResponse supports HTTP Range requests required for video/audio seeking
    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={
            'Content-Disposition': 'inline',  # Forces inline display, not download
            'Accept-Ranges': 'bytes',  # Explicitly indicate we support range requests
        }
    )


@app.get("/api/documents/{doc_id}/file")
async def get_document_file(doc_id: str, request: Request):
    """Get the actual document file for inline viewing

    Note: Returns 404 for hidden documents or documents in hidden categories.
    """
    client_ip, request_id = get_client_info(request)

    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    doc = await asyncio.to_thread(db.get_document, doc_id, include_hidden=False)
    if not doc:
        security_logger.log_security_event(
            event_type="file_not_found",
            severity="low",
            client_ip=client_ip,
            message=f"Attempted file access for non-existent or hidden document: {doc_id}",
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=404, detail="Document not found")

    return _serve_document_file(doc, doc_id, client_ip, request_id)


@app.get("/api/admin/documents/{doc_id}/file")
async def admin_get_document_file(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Serve a document file for admin review — INCLUDING hidden documents.

    Admin-authenticated twin of /api/documents/{doc_id}/file that bypasses the
    is_hidden / hidden-category filter so admins can preview what they've hidden.
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    client_ip, request_id = get_client_info(request)
    doc = await asyncio.to_thread(db.get_document, doc_id, include_hidden=True)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return _serve_document_file(doc, doc_id, client_ip, request_id, admin=True)


@app.get("/api/admin/documents/{doc_id}/text")
async def admin_get_document_text(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Full text of a document for admin review, including hidden documents."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    full_text = await asyncio.to_thread(db.get_document_full_text, doc_id, include_hidden=True)
    if full_text is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"full_text": full_text}


def generate_pdf_thumbnail(pdf_path: Path, output_path: Path) -> bool:
    """Generate a thumbnail from the first page of a PDF using PyMuPDF"""
    try:
        import fitz  # PyMuPDF
        
        doc = fitz.open(str(pdf_path))
        if doc.page_count == 0:
            doc.close()
            return False
        
        page = doc[0]  # First page
        
        # Calculate zoom to fit thumbnail dimensions
        page_rect = page.rect
        zoom_x = THUMBNAIL_WIDTH / page_rect.width
        zoom_y = THUMBNAIL_HEIGHT / page_rect.height
        zoom = min(zoom_x, zoom_y)
        
        # Create a transformation matrix
        mat = fitz.Matrix(zoom, zoom)
        
        # Render page to pixmap
        pix = page.get_pixmap(matrix=mat, alpha=False)
        
        # Save as JPEG
        pix.save(str(output_path))
        
        doc.close()
        return True
    except Exception as e:
        print(f"Error generating PDF thumbnail: {e}")
        return False


def generate_image_thumbnail(image_path: Path, output_path: Path) -> bool:
    """Generate a thumbnail from an image file"""
    try:
        from PIL import Image
        
        with Image.open(str(image_path)) as img:
            # Convert to RGB if necessary (handles RGBA, P mode, etc.)
            if img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Create thumbnail maintaining aspect ratio
            img.thumbnail((THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), Image.Resampling.LANCZOS)
            
            # Save as JPEG
            img.save(str(output_path), 'JPEG', quality=85)
        
        return True
    except Exception as e:
        print(f"Error generating image thumbnail: {e}")
        return False


def generate_video_thumbnail(video_path: Path, output_path: Path) -> bool:
    """Generate a thumbnail from a video file by extracting a frame"""
    try:
        import cv2
        from PIL import Image
        
        # Open the video file
        cap = cv2.VideoCapture(str(video_path))
        
        if not cap.isOpened():
            print(f"Error: Could not open video file: {video_path}")
            return False
        
        # Get total frame count
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        # Try to extract a frame from ~10% into the video (avoids black intro frames)
        target_frame = max(1, int(total_frames * 0.1))
        
        # If video is very short, just use the first frame
        if total_frames < 10:
            target_frame = 0
        
        # Seek to target frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        
        # Read the frame
        ret, frame = cap.read()
        cap.release()
        
        if not ret or frame is None:
            print(f"Error: Could not read frame from video: {video_path}")
            return False
        
        # Convert BGR (OpenCV format) to RGB (PIL format)
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Convert to PIL Image
        img = Image.fromarray(frame_rgb)
        
        # Create thumbnail maintaining aspect ratio
        img.thumbnail((THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), Image.Resampling.LANCZOS)
        
        # Save as JPEG
        img.save(str(output_path), 'JPEG', quality=85)
        
        return True
    except Exception as e:
        print(f"Error generating video thumbnail: {e}")
        return False


def create_placeholder_thumbnail(file_type: str, output_path: Path) -> bool:
    """Create a placeholder thumbnail for audio/video files"""
    try:
        from PIL import Image, ImageDraw
        
        # Create a simple colored rectangle with icon
        img = Image.new('RGB', (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), color='#2a2a40')
        draw = ImageDraw.Draw(img)
        
        # Draw icon based on file type
        icon_color = '#6366f1'  # Indigo
        
        if file_type == 'audio':
            # Draw music note symbol
            cx, cy = THUMBNAIL_WIDTH // 2, THUMBNAIL_HEIGHT // 2
            # Simple circle for note head
            draw.ellipse([cx-20, cy-10, cx+10, cy+20], fill=icon_color)
            draw.ellipse([cx+10, cy-30, cx+40, cy], fill=icon_color)
            # Stems
            draw.rectangle([cx+5, cy-60, cx+12, cy+5], fill=icon_color)
            draw.rectangle([cx+35, cy-80, cx+42, cy-15], fill=icon_color)
            # Connecting bar
            draw.rectangle([cx+5, cy-80, cx+42, cy-68], fill=icon_color)
        elif file_type == 'video':
            # Draw play button triangle
            cx, cy = THUMBNAIL_WIDTH // 2, THUMBNAIL_HEIGHT // 2
            points = [(cx-25, cy-35), (cx-25, cy+35), (cx+35, cy)]
            draw.polygon(points, fill=icon_color)
            # Draw circle around it
            draw.ellipse([cx-50, cy-50, cx+50, cy+50], outline=icon_color, width=4)
        else:
            # Generic document icon
            cx, cy = THUMBNAIL_WIDTH // 2, THUMBNAIL_HEIGHT // 2
            # Document shape
            draw.rectangle([cx-35, cy-50, cx+35, cy+50], outline=icon_color, width=3)
            # Lines for text
            for i in range(4):
                y = cy - 30 + i * 20
                draw.line([(cx-25, y), (cx+25, y)], fill=icon_color, width=2)
        
        img.save(str(output_path), 'JPEG', quality=85)
        return True
    except Exception as e:
        print(f"Error creating placeholder thumbnail: {e}")
        return False


def _generate_thumbnail_sync(
    file_path: Path,
    file_type: str,
    thumbnail_path: Path,
    base_path: Path,
) -> None:
    """Synchronous thumbnail generation (run in thread pool to avoid blocking)."""
    if not file_path.exists():
        create_placeholder_thumbnail("document", thumbnail_path)
        return
    if file_type == "pdf":
        if not generate_pdf_thumbnail(file_path, thumbnail_path):
            create_placeholder_thumbnail("document", thumbnail_path)
    elif file_type in ("image", "document"):
        if not generate_image_thumbnail(file_path, thumbnail_path):
            create_placeholder_thumbnail("document", thumbnail_path)
    elif file_type == "audio":
        create_placeholder_thumbnail("audio", thumbnail_path)
    elif file_type == "video":
        if not generate_video_thumbnail(file_path, thumbnail_path):
            create_placeholder_thumbnail("video", thumbnail_path)
    else:
        create_placeholder_thumbnail("document", thumbnail_path)


@app.get("/api/documents/{doc_id}/thumbnail")
async def get_document_thumbnail(doc_id: str, request: Request):
    """Get a thumbnail preview of a document
    
    Thumbnail generation runs in a thread pool so the event loop is not blocked.
    Note: Returns 404 for hidden documents or documents in hidden categories.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Ensure thumbnails directory exists
    THUMBNAILS_PATH.mkdir(exist_ok=True)
    
    thumbnail_path = THUMBNAILS_PATH / f"{doc_id}.jpg"
    
    # Fast path: if the thumbnail already exists on disk, serve it immediately
    # without querying the database. The browse list already verified visibility
    # when it returned this doc_id, so a redundant DB check is unnecessary.
    if thumbnail_path.exists():
        return FileResponse(
            path=thumbnail_path,
            media_type="image/jpeg",
        )
    
    doc = await asyncio.to_thread(db.get_document, doc_id, include_hidden=False, include_full_text=False)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    file_path = (BASE_PATH / doc["path"]).resolve()
    file_type = doc.get("file_type", "pdf")
    
    if not str(file_path).startswith(str(BASE_PATH.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Run thumbnail generation in thread pool to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        _generate_thumbnail_sync,
        file_path,
        file_type,
        thumbnail_path,
        BASE_PATH,
    )
    
    if thumbnail_path.exists():
        return FileResponse(
            path=thumbnail_path,
            media_type="image/jpeg",
        )
    
    raise HTTPException(status_code=404, detail="Could not generate thumbnail")


@app.post("/api/ask")
async def ask_question(ask_request: AskRequest, request: Request):
    """Ask a question and get an AI-powered answer
    
    Note: Hidden documents are excluded from AI context.
    """
    client_ip, request_id = get_client_info(request)
    
    if not llm or not llm.is_available():
        raise HTTPException(
            status_code=503, 
            detail="LLM not configured. Set OPENAI_API_KEY environment variable."
        )
    
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    
    def _do_ask_context():
        context_docs = vector_store.search(
            query=ask_request.question, n_results=ask_request.num_context_docs,
            category=ask_request.category
        )
        visible = [doc for doc in context_docs if db.is_document_visible(doc.get("id", ""))]
        enriched = []
        for doc in visible:
            doc_id = doc.get("id")
            if doc_id and db:
                full_doc = db.get_document(doc_id, include_hidden=False)
                if full_doc:
                    enriched.append({
                        **doc,
                        "full_text": full_doc.get("full_text", doc.get("text", "")),
                        "filename": full_doc.get("filename", doc.get("filename", "Unknown")),
                        "category": full_doc.get("category", doc.get("category", "Unknown")),
                        "subcategory": full_doc.get("subcategory", doc.get("subcategory", ""))
                    })
        return visible, enriched

    visible_context_docs, enriched_docs = await asyncio.to_thread(_do_ask_context)

    security_logger.log_llm_query(
        client_ip=client_ip, question=ask_request.question,
        context_docs_count=len(visible_context_docs),
        request_id=request_id, category=ask_request.category
    )
    
    if not visible_context_docs:
        return {
            "question": ask_request.question,
            "answer": "No relevant documents found for this question.",
            "sources": []
        }
    
    # Get answer from LLM with enriched context
    try:
        answer = llm.answer_question(ask_request.question, enriched_docs)
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
        for doc in enriched_docs
    ]
    
    return {
        "question": ask_request.question,
        "answer": answer,
        "sources": sources
    }


@app.post("/api/ask/stream")
async def ask_question_stream(ask_request: AskRequest, request: Request):
    """Ask a question and stream the response
    
    Note: Hidden documents are excluded from AI context.
    """
    client_ip, request_id = get_client_info(request)
    
    if not llm or not llm.is_available():
        raise HTTPException(status_code=503, detail="LLM not configured")
    
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    
    def _do_stream_context():
        ctx = vector_store.search(
            query=ask_request.question, n_results=ask_request.num_context_docs,
            category=ask_request.category
        )
        visible = [d for d in ctx if db.is_document_visible(d.get("id", ""))]
        enriched = []
        for d in visible:
            did = d.get("id")
            if did and db:
                fd = db.get_document(did, include_hidden=False)
                if fd:
                    enriched.append({
                        **d, "full_text": fd.get("full_text", d.get("text", "")),
                        "filename": fd.get("filename", d.get("filename", "Unknown")),
                        "category": fd.get("category", d.get("category", "Unknown")),
                        "subcategory": fd.get("subcategory", d.get("subcategory", ""))
                    })
        return enriched

    enriched_docs = await asyncio.to_thread(_do_stream_context)

    # Log the streaming LLM query
    security_logger.log_llm_query(
        client_ip=client_ip,
        question=ask_request.question,
        context_docs_count=len(enriched_docs),
        request_id=request_id,
        category=ask_request.category,
        streaming=True
    )
    
    async def generate():
        for chunk in llm.answer_question(ask_request.question, enriched_docs, stream=True):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/documents/{doc_id}/summary")
async def get_document_summary(doc_id: str, request: Request, regenerate: bool = False):
    """Get an AI-generated summary of a document (cached if available)
    
    Note: Returns 404 for hidden documents or documents in hidden categories.
    """
    client_ip, request_id = get_client_info(request)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    doc = await asyncio.to_thread(db.get_document, doc_id, include_hidden=False)
    if not doc:
        security_logger.log_security_event(
            event_type="summary_not_found",
            severity="low",
            client_ip=client_ip,
            message=f"Summary requested for non-existent or hidden document: {doc_id}",
            request_id=request_id,
            document_id=doc_id
        )
        raise HTTPException(status_code=404, detail="Document not found")
    
    if not regenerate:
        cached = await asyncio.to_thread(db.get_summary, doc_id)
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
        await asyncio.to_thread(db.save_summary, doc_id, summary)
        
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


# =============================================================================
# ADMIN TELEMETRY ENDPOINTS
# =============================================================================

LOG_DIR = BASE_PATH / "logs"


# Telemetry reads run on a dedicated, bounded thread pool so heavy aggregations over the
# large telemetry table can never exhaust the default thread pool that serves public requests.
_telemetry_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telemetry")

async def _run_tel(fn, *args):
    """Run a telemetry function on the dedicated bounded telemetry pool."""
    return await asyncio.get_event_loop().run_in_executor(_telemetry_executor, fn, *args)

def _tel(sql: str, params: tuple = ()) -> list:
    """Shorthand: run a telemetry query against the database, return list of dicts."""
    if not db:
        return []
    return db.query_telemetry(sql, params)

async def _atel(sql: str, params: tuple = ()) -> list:
    """Async version of _tel — runs on the dedicated telemetry pool."""
    if not db:
        return []
    return await _run_tel(db.query_telemetry, sql, params)


def _json_val(col: str, key: str) -> str:
    """Build a json_extract expression for the data column."""
    return f"json_extract({col}, '$.{key}')"


@app.get("/admin")
async def admin_console():
    """Serve the admin console page"""
    admin_path = STATIC_PATH / "admin.html"
    if admin_path.exists():
        return FileResponse(admin_path)
    raise HTTPException(status_code=404, detail="Admin console not found")


# Protected API Documentation (admin only)
@app.get("/docs", include_in_schema=False)
async def get_docs(request: Request, x_api_key: str = Header(None)):
    """Protected Swagger UI documentation - requires admin authentication"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail="Admin authentication required to view API docs")
    
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=app.title + " - API Docs"
    )


@app.get("/redoc", include_in_schema=False)
async def get_redoc(request: Request, x_api_key: str = Header(None)):
    """Protected ReDoc documentation - requires admin authentication"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail="Admin authentication required to view API docs")
    
    from fastapi.openapi.docs import get_redoc_html
    return get_redoc_html(
        openapi_url="/openapi.json",
        title=app.title + " - API Docs"
    )


@app.get("/openapi.json", include_in_schema=False)
async def get_openapi(request: Request, x_api_key: str = Header(None)):
    """Protected OpenAPI schema - requires admin authentication"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail="Admin authentication required to view API schema")
    
    from fastapi.openapi.utils import get_openapi
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes
    )


@app.post("/api/admin/login")
async def admin_login(request: Request, x_api_key: str = Header(None)):
    """
    Verify admin credentials and return access status.
    The API key should be sent in the X-API-Key header.
    """
    client_ip, request_id = get_client_info(request)
    
    is_authorized, error = verify_admin_access(request, x_api_key)
    
    if not is_authorized:
        security_logger.log_security_event(
            event_type="admin_login_failed",
            severity="high",
            client_ip=client_ip,
            message=f"Admin login failed: {error}",
            request_id=request_id
        )
        raise HTTPException(status_code=401, detail=error)
    
    # Log successful login
    security_logger.log_security_event(
        event_type="admin_login_success",
        severity="info",
        client_ip=client_ip,
        message="Admin login successful",
        request_id=request_id
    )
    
    return {
        "success": True,
        "message": "Authentication successful",
        "client_ip": client_ip
    }


@app.get("/api/admin/verify")
async def admin_verify(request: Request, x_api_key: str = Header(None)):
    """Verify if current credentials are valid without logging"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    return {"valid": True}


@app.get("/api/admin/telemetry/overview")
async def get_telemetry_overview(request: Request, x_api_key: str = Header(None)):
    """Get overview telemetry statistics (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    cached = _admin_cache.get("telemetry_overview")
    if cached:
        return cached

    def _build():
        now = datetime.utcnow()
        hour_ago = (now - timedelta(hours=1)).isoformat()
        day_ago = (now - timedelta(days=1)).isoformat()

        r = lambda sql, p=(): (_tel(sql, p) or [{}])[0]

        total_requests = r("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='access'").get("c", 0)
        requests_last_hour = r("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=?", (hour_ago,)).get("c", 0)
        requests_last_day = r("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=?", (day_ago,)).get("c", 0)

        avg_row = r("SELECT AVG(duration_ms) AS a FROM telemetry_events WHERE log_source='access' AND duration_ms IS NOT NULL")
        avg_duration = avg_row.get("a") or 0

        err_count = r("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND status_code>=400").get("c", 0)
        error_rate = (err_count / total_requests * 100) if total_requests > 0 else 0

        unique_ips = r("SELECT COUNT(DISTINCT client_ip) AS c FROM telemetry_events WHERE log_source='access'").get("c", 0)
        unique_ips_hour = r("SELECT COUNT(DISTINCT client_ip) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=?", (hour_ago,)).get("c", 0)

        status_rows = _tel("SELECT CAST(status_code AS TEXT) AS code, COUNT(*) AS c FROM telemetry_events WHERE log_source='access' GROUP BY status_code")
        status_codes = {row["code"]: row["c"] for row in status_rows}

        top_ep = _tel("SELECT path, COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND path IS NOT NULL GROUP BY path ORDER BY c DESC LIMIT 10")

        sec_total = r("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='security'").get("c", 0)
        sec_high = r("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='security' AND severity IN ('high','critical')").get("c", 0)

        rate_limited = r(f"SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND {_json_val('data','rate_limited')} IS NOT NULL").get("c", 0)
        error_count = r("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='error'").get("c", 0)

        from security_logger import get_session_stats, get_blocked_ips, get_blocked_sessions
        session_stats = get_session_stats()

        return {
            "generated_at": now.isoformat(),
            "overview": {
                "total_requests": total_requests,
                "requests_last_hour": requests_last_hour,
                "requests_last_day": requests_last_day,
                "avg_response_time_ms": round(avg_duration, 2),
                "error_rate_percent": round(error_rate, 2),
                "unique_visitors": unique_ips,
                "unique_visitors_hour": unique_ips_hour,
                "security_events": sec_total,
                "security_events_high": sec_high,
                "rate_limited_requests": rate_limited,
                "error_count": error_count
            },
            "status_codes": status_codes,
            "top_endpoints": [{"path": row["path"], "count": row["c"]} for row in top_ep],
            "sessions": {
                "active_sessions": session_stats.get("active_sessions", 0),
                "blocked_sessions": len(get_blocked_sessions()),
                "blocked_ips": len(get_blocked_ips())
            }
        }

    result = await _run_tel(_build)
    _admin_cache.set("telemetry_overview", result)
    return result


@app.get("/api/admin/telemetry/requests")
async def get_request_telemetry(
    request: Request,
    timeframe: str = "1h",
    x_api_key: str = Header(None)
):
    """Get request telemetry with time series data (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    now = datetime.utcnow()
    tf_map = {"1h": (1, 1), "6h": (6, 5), "24h": (24, 15), "7d": (168, 60)}
    hours, bucket_min = tf_map.get(timeframe, (1, 1))
    cutoff = (now - timedelta(hours=hours)).isoformat()

    total_row = await _atel("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=?", (cutoff,))
    total = total_row[0]["c"] if total_row else 0

    # Time series buckets via SQL
    fmt = f"strftime('%Y-%m-%dT%H:', timestamp) || printf('%02d', (CAST(strftime('%M', timestamp) AS INTEGER) / {bucket_min}) * {bucket_min}) || ':00'"
    ts_rows = await _atel(
        f"SELECT {fmt} AS bucket, COUNT(*) AS requests, "
        f"SUM(CASE WHEN status_code>=400 THEN 1 ELSE 0 END) AS errors, "
        f"AVG(duration_ms) AS avg_dur "
        f"FROM telemetry_events WHERE log_source='access' AND timestamp>=? "
        f"GROUP BY bucket ORDER BY bucket", (cutoff,))
    time_series = [{"time": r["bucket"], "requests": r["requests"], "errors": r["errors"],
                     "avg_duration_ms": round(r["avg_dur"] or 0, 2)} for r in ts_rows]

    # Method breakdown
    method_rows = await _atel("SELECT method, COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=? GROUP BY method", (cutoff,))
    methods = {r["method"] or "UNKNOWN": r["c"] for r in method_rows}

    # Response time distribution
    dur_rows = await _atel(
        "SELECT "
        "SUM(CASE WHEN duration_ms<50 THEN 1 ELSE 0 END) AS d0, "
        "SUM(CASE WHEN duration_ms>=50 AND duration_ms<100 THEN 1 ELSE 0 END) AS d1, "
        "SUM(CASE WHEN duration_ms>=100 AND duration_ms<500 THEN 1 ELSE 0 END) AS d2, "
        "SUM(CASE WHEN duration_ms>=500 AND duration_ms<1000 THEN 1 ELSE 0 END) AS d3, "
        "SUM(CASE WHEN duration_ms>=1000 AND duration_ms<5000 THEN 1 ELSE 0 END) AS d4, "
        "SUM(CASE WHEN duration_ms>=5000 THEN 1 ELSE 0 END) AS d5 "
        "FROM telemetry_events WHERE log_source='access' AND timestamp>=? AND duration_ms IS NOT NULL", (cutoff,))
    d = dur_rows[0] if dur_rows else {}
    duration_buckets = {"0-50ms": d.get("d0") or 0, "50-100ms": d.get("d1") or 0,
                        "100-500ms": d.get("d2") or 0, "500ms-1s": d.get("d3") or 0,
                        "1s-5s": d.get("d4") or 0, "5s+": d.get("d5") or 0}

    # Recent 50 requests
    recent_rows = await _atel(
        f"SELECT timestamp, client_ip, path, method, status_code, duration_ms, "
        f"{_json_val('data','user_agent')} AS user_agent "
        f"FROM telemetry_events WHERE log_source='access' AND timestamp>=? "
        f"ORDER BY timestamp DESC LIMIT 50", (cutoff,))
    recent_requests = [{"timestamp": r["timestamp"], "client_ip": r["client_ip"] or "unknown",
                         "path": r["path"] or "", "method": r["method"] or "",
                         "status_code": r["status_code"], "duration_ms": r["duration_ms"],
                         "user_agent": (r["user_agent"] or "")[:150]} for r in recent_rows]

    await enrich_with_geo(recent_requests, 'client_ip', limit=30)

    return {
        "timeframe": timeframe, "total_requests": total,
        "time_series": time_series, "methods": methods,
        "response_time_distribution": duration_buckets,
        "recent_requests": recent_requests
    }


@app.get("/api/admin/telemetry/search")
async def get_search_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get search-specific telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    jv = lambda k: _json_val('data', k)

    total_row = await _atel("SELECT COUNT(*) AS c FROM telemetry_events WHERE event_type='search_query'")
    total = total_row[0]["c"] if total_row else 0

    st_rows = await _atel(f"SELECT {jv('search_type')} AS st, COUNT(*) AS c FROM telemetry_events WHERE event_type='search_query' GROUP BY st")
    search_types = {r["st"] or "unknown": r["c"] for r in st_rows}

    tq_rows = await _atel(f"SELECT SUBSTR({jv('query')},1,100) AS q, COUNT(*) AS c FROM telemetry_events WHERE event_type='search_query' AND {jv('query')} IS NOT NULL GROUP BY q ORDER BY c DESC LIMIT 20")
    top_queries = [{"query": r["q"], "count": r["c"]} for r in tq_rows]

    cat_rows = await _atel(f"SELECT {jv('category')} AS cat, COUNT(*) AS c FROM telemetry_events WHERE event_type='search_query' AND {jv('category')} IS NOT NULL GROUP BY cat")
    category_usage = {r["cat"]: r["c"] for r in cat_rows}

    ft_rows = await _atel(f"SELECT {jv('file_type')} AS ft, COUNT(*) AS c FROM telemetry_events WHERE event_type='search_query' AND {jv('file_type')} IS NOT NULL GROUP BY ft")
    file_type_usage = {r["ft"]: r["c"] for r in ft_rows}

    avg_row = await _atel(f"SELECT AVG(CAST({jv('result_count')} AS REAL)) AS a, SUM(CASE WHEN CAST({jv('result_count')} AS INTEGER)=0 THEN 1 ELSE 0 END) AS z FROM telemetry_events WHERE event_type='search_query'")
    a = avg_row[0] if avg_row else {}

    return {
        "total_searches": total,
        "search_types": search_types,
        "top_queries": top_queries,
        "category_usage": category_usage,
        "file_type_usage": file_type_usage,
        "avg_results_per_search": round(a.get("a") or 0, 2),
        "zero_result_searches": a.get("z") or 0
    }


@app.get("/api/admin/telemetry/search/log")
async def get_search_log(
    request: Request,
    x_api_key: str = Header(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    q: str = Query(None, description="Filter by query text"),
    search_type: str = Query(None, description="Exact match on search type"),
    category: str = Query(None, description="Exact match on category"),
    ip: str = Query(None, description="Partial match on client IP"),
    min_results: int = Query(None, description="Minimum result count"),
    max_results: int = Query(None, description="Maximum result count"),
):
    """Get paginated list of individual search queries (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    jv = lambda k: _json_val('data', k)
    offset = (page - 1) * per_page

    where = "event_type='search_query'"
    params: list = []
    if q:
        where += f" AND {jv('query')} LIKE ?"
        params.append(f"%{q}%")
    if search_type:
        where += f" AND {jv('search_type')} = ?"
        params.append(search_type)
    if category:
        where += f" AND {jv('category')} = ?"
        params.append(category)
    if ip:
        where += " AND client_ip LIKE ?"
        params.append(f"%{ip}%")
    if min_results is not None:
        where += f" AND CAST({jv('result_count')} AS INTEGER) >= ?"
        params.append(min_results)
    if max_results is not None:
        where += f" AND CAST({jv('result_count')} AS INTEGER) <= ?"
        params.append(max_results)

    count_row = await _atel(f"SELECT COUNT(*) AS c FROM telemetry_events WHERE {where}", tuple(params))
    total = count_row[0]["c"] if count_row else 0

    rows = await _atel(
        f"SELECT timestamp, client_ip, "
        f"{jv('query')} AS query, "
        f"{jv('search_type')} AS search_type, "
        f"{jv('result_count')} AS result_count, "
        f"{jv('category')} AS category, "
        f"{jv('file_type')} AS file_type "
        f"FROM telemetry_events WHERE {where} "
        f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        tuple(params) + (per_page, offset)
    )

    searches = []
    for r in rows:
        searches.append({
            "timestamp": r.get("timestamp"),
            "client_ip": r.get("client_ip"),
            "query": r.get("query"),
            "search_type": r.get("search_type"),
            "result_count": int(r.get("result_count") or 0),
            "category": r.get("category"),
            "file_type": r.get("file_type"),
        })

    return {
        "searches": searches,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if per_page else 0,
    }


@app.get("/api/admin/telemetry/documents")
async def get_document_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get document access telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    jv = lambda k: _json_val('data', k)

    total_row = await _atel("SELECT COUNT(*) AS c FROM telemetry_events WHERE event_type='document_access'")
    total = total_row[0]["c"] if total_row else 0

    at_rows = await _atel(f"SELECT {jv('action')} AS a, COUNT(*) AS c FROM telemetry_events WHERE event_type='document_access' GROUP BY a")
    access_types = {r["a"] or "unknown": r["c"] for r in at_rows}

    td_rows = await _atel(f"SELECT {jv('filename')} AS fn, COUNT(*) AS c FROM telemetry_events WHERE event_type='document_access' AND {jv('filename')} IS NOT NULL GROUP BY fn ORDER BY c DESC LIMIT 20")
    top_docs = [{"filename": r["fn"], "count": r["c"]} for r in td_rows]

    ft_rows = await _atel(f"SELECT {jv('file_type')} AS ft, COUNT(*) AS c FROM telemetry_events WHERE event_type='document_access' GROUP BY ft")
    file_types = {(r["ft"] or ".pdf"): r["c"] for r in ft_rows}

    bytes_row = await _atel(f"SELECT COALESCE(SUM(CAST({jv('file_size_bytes')} AS INTEGER)),0) AS b FROM telemetry_events WHERE event_type='document_access'")
    total_bytes = bytes_row[0]["b"] if bytes_row else 0

    return {
        "total_document_accesses": total,
        "access_types": access_types,
        "top_documents": top_docs,
        "file_types": file_types,
        "total_data_served_mb": round(total_bytes / (1024 * 1024), 2)
    }


@app.get("/api/admin/telemetry/ai")
async def get_ai_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get AI/LLM usage telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    def _build():
        jv = lambda k: _json_val('data', k)
        llm_total = (_tel("SELECT COUNT(*) AS c FROM telemetry_events WHERE event_type='llm_query'") or [{}])[0].get("c", 0)
        sum_total = (_tel(f"SELECT COUNT(*) AS c FROM telemetry_events WHERE event_type='document_access' AND {jv('action')} IN ('summary_generate','summary_cached')") or [{}])[0].get("c", 0)
        streaming = (_tel(f"SELECT COUNT(*) AS c FROM telemetry_events WHERE event_type='llm_query' AND {jv('streaming')}=1") or [{}])[0].get("c", 0)
        tq_rows = _tel(f"SELECT SUBSTR({jv('question')},1,100) AS q, COUNT(*) AS c FROM telemetry_events WHERE event_type='llm_query' AND {jv('question')} IS NOT NULL GROUP BY q ORDER BY c DESC LIMIT 15")
        return {
            "total_ai_queries": llm_total,
            "total_summaries": sum_total,
            "streaming_queries": streaming,
            "non_streaming_queries": llm_total - streaming,
            "top_questions": [{"question": r["q"], "count": r["c"]} for r in tq_rows]
        }

    return await _run_tel(_build)


@app.get("/api/admin/telemetry/security")
async def get_security_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get security events telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    def _build():
        jv = lambda k: _json_val('data', k)
        total = (_tel("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='security'") or [{}])[0].get("c", 0)
        et_rows = _tel("SELECT event_type, COUNT(*) AS c FROM telemetry_events WHERE log_source='security' GROUP BY event_type")
        event_types = {r["event_type"]: r["c"] for r in et_rows}
        sev_rows = _tel("SELECT severity, COUNT(*) AS c FROM telemetry_events WHERE log_source='security' GROUP BY severity")
        severities = {(r["severity"] or "unknown"): r["c"] for r in sev_rows}
        hs_rows = _tel(f"SELECT timestamp, event_type, SUBSTR({jv('message')},1,200) AS message, client_ip FROM telemetry_events WHERE log_source='security' AND severity IN ('high','critical') ORDER BY timestamp DESC LIMIT 20")
        high_severity = [{"timestamp": r["timestamp"], "event_type": r["event_type"], "message": r["message"] or "", "client_ip": r["client_ip"]} for r in hs_rows]
        rl = (_tel("SELECT COUNT(*) AS c FROM telemetry_events WHERE event_type='rate_limit_exceeded'") or [{}])[0].get("c", 0)
        sa = (_tel("SELECT COUNT(*) AS c FROM telemetry_events WHERE event_type='suspicious_activity'") or [{}])[0].get("c", 0)
        ip_rows = _tel("SELECT client_ip, COUNT(*) AS c FROM telemetry_events WHERE log_source='security' AND client_ip IS NOT NULL GROUP BY client_ip ORDER BY c DESC LIMIT 10")
        from security_logger import get_blocked_ips, get_blocked_sessions
        return {
            "total_security_events": total,
            "event_types": event_types,
            "severities": severities,
            "recent_high_severity": high_severity,
            "rate_limit_violations": rl,
            "suspicious_activities": sa,
            "top_ips_by_events": [{"ip": r["client_ip"], "count": r["c"]} for r in ip_rows],
            "blocked_ips": list(get_blocked_ips()),
            "blocked_sessions": len(get_blocked_sessions())
        }

    return await _run_tel(_build)


@app.get("/api/admin/telemetry/errors")
async def get_error_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get error logs telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    def _build():
        jv = lambda k: _json_val('data', k)
        total = (_tel("SELECT COUNT(*) AS c FROM telemetry_events WHERE log_source='error'") or [{}])[0].get("c", 0)
        et_rows = _tel(f"SELECT {jv('error_type')} AS et, COUNT(*) AS c FROM telemetry_events WHERE log_source='error' GROUP BY et")
        error_types = {(r["et"] or "unknown"): r["c"] for r in et_rows}
        ctx_rows = _tel(f"SELECT {jv('context')} AS ctx, COUNT(*) AS c FROM telemetry_events WHERE log_source='error' GROUP BY ctx")
        contexts = {(r["ctx"] or "unknown"): r["c"] for r in ctx_rows}
        rec_rows = _tel(f"SELECT timestamp, {jv('error_type')} AS error_type, {jv('context')} AS context, SUBSTR({jv('error_message')},1,200) AS message FROM telemetry_events WHERE log_source='error' ORDER BY timestamp DESC LIMIT 20")
        recent_errors = [{"timestamp": r["timestamp"], "error_type": r["error_type"], "context": r["context"], "message": r["message"] or ""} for r in rec_rows]
        return {
            "total_errors": total,
            "error_types": error_types,
            "error_contexts": contexts,
            "recent_errors": recent_errors
        }

    return await _run_tel(_build)


@app.get("/api/admin/telemetry/visitors")
async def get_visitor_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get visitor/user analytics (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    def _build():
        jv = lambda k: _json_val('data', k)
        now = datetime.utcnow()
        day_ago = (now - timedelta(days=1)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()

        uv_today = (_tel("SELECT COUNT(DISTINCT client_ip) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=?", (day_ago,)) or [{}])[0].get("c", 0)
        uv_week = (_tel("SELECT COUNT(DISTINCT client_ip) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=?", (week_ago,)) or [{}])[0].get("c", 0)

        dv_rows = _tel("SELECT SUBSTR(timestamp,1,10) AS d, COUNT(DISTINCT client_ip) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=? GROUP BY d ORDER BY d", (week_ago,))
        daily_unique = [{"date": r["d"], "unique_visitors": r["c"]} for r in dv_rows]

        ua_rows = _tel(f"SELECT {jv('user_agent')} AS ua FROM telemetry_events WHERE log_source='access' AND timestamp>=? AND {jv('user_agent')} IS NOT NULL", (day_ago,))
        user_agents = {}
        for r in ua_rows:
            ua = (r["ua"] or "")[:100]
            if not ua:
                continue
            if "Chrome" in ua: browser = "Chrome"
            elif "Firefox" in ua: browser = "Firefox"
            elif "Safari" in ua: browser = "Safari"
            elif "curl" in ua: browser = "curl/CLI"
            elif "bot" in ua.lower() or "spider" in ua.lower(): browser = "Bot/Crawler"
            else: browser = "Other"
            user_agents[browser] = user_agents.get(browser, 0) + 1

        from urllib.parse import urlparse
        ref_rows = _tel(f"SELECT {jv('referer')} AS ref FROM telemetry_events WHERE log_source='access' AND timestamp>=? AND {jv('referer')} IS NOT NULL", (day_ago,))
        referrers = {}
        for r in ref_rows:
            ref = r["ref"] or ""
            if ref and "localhost" not in ref and "127.0.0.1" not in ref:
                try:
                    domain = urlparse(ref).netloc or "Direct"
                except Exception:
                    domain = "Direct"
                referrers[domain] = referrers.get(domain, 0) + 1
        top_referrers = sorted(referrers.items(), key=lambda x: x[1], reverse=True)[:10]

        ip_rows = _tel("SELECT client_ip, COUNT(*) AS c FROM telemetry_events WHERE log_source='access' AND timestamp>=? AND client_ip IS NOT NULL AND client_ip!='unknown' GROUP BY client_ip ORDER BY c DESC LIMIT 20", (day_ago,))
        top_ips = [{"ip": r["client_ip"], "client_ip": r["client_ip"], "count": r["c"]} for r in ip_rows]

        return uv_today, uv_week, daily_unique, user_agents, top_referrers, top_ips

    uv_today, uv_week, daily_unique, user_agents, top_referrers, top_ips = await _run_tel(_build)
    await enrich_with_geo(top_ips, 'client_ip', limit=20)

    return {
        "unique_visitors_today": uv_today,
        "unique_visitors_week": uv_week,
        "daily_unique_visitors": daily_unique,
        "browsers": user_agents,
        "top_referrers": [{"domain": d, "count": c} for d, c in top_referrers],
        "top_ips": top_ips
    }


@app.get("/api/admin/telemetry/system")
async def get_system_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get system health and status information (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    import psutil
    import platform
    
    # System info
    system_info = {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "python_version": platform.python_version(),
    }
    
    # Memory usage
    memory = psutil.virtual_memory()
    memory_info = {
        "total_gb": round(memory.total / (1024**3), 2),
        "available_gb": round(memory.available / (1024**3), 2),
        "used_percent": memory.percent
    }
    
    # Disk usage
    disk = psutil.disk_usage('/')
    disk_info = {
        "total_gb": round(disk.total / (1024**3), 2),
        "free_gb": round(disk.free / (1024**3), 2),
        "used_percent": round(disk.percent, 1)
    }
    
    # CPU
    cpu_info = {
        "cores": psutil.cpu_count(),
        "usage_percent": psutil.cpu_percent(interval=0.1)
    }
    
    db_stats = await asyncio.to_thread(db.get_stats) if db else {}
    
    log_sizes = {}
    for log_file in ["access.log", "security.log", "audit.log", "error.log"]:
        log_path = LOG_DIR / log_file
        if log_path.exists():
            log_sizes[log_file] = round(log_path.stat().st_size / (1024 * 1024), 2)
    
    first_row = await _atel("SELECT timestamp FROM telemetry_events WHERE log_source='access' ORDER BY timestamp ASC LIMIT 1")
    first_request = first_row[0]["timestamp"] if first_row else None
    
    return {
        "system": system_info,
        "memory": memory_info,
        "disk": disk_info,
        "cpu": cpu_info,
        "database": {
            "total_documents": db_stats.get("total_documents", 0),
            "total_pages": db_stats.get("total_pages", 0),
            "vector_chunks": vector_store.get_count() if vector_store else 0
        },
        "log_sizes_mb": log_sizes,
        "llm_available": llm.is_available() if llm else False,
        "auto_index_enabled": AUTO_INDEX_ENABLED,
        "is_indexing": is_indexing,
        "last_index_time": last_index_time.isoformat() if last_index_time else None
    }


# =============================================================================
# ADMIN LOG MANAGEMENT ENDPOINTS
# =============================================================================

@app.post("/api/admin/logs/clear")
async def clear_logs(
    request: Request,
    log_type: str = Query(..., description="Log type to clear: access, security, audit, error, or all"),
    x_api_key: str = Header(None)
):
    """Clear specified log files (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    client_ip, request_id = get_client_info(request)
    
    # Map log types to files
    log_files = {
        "access": "access.log",
        "security": "security.log",
        "audit": "audit.log",
        "error": "error.log"
    }
    
    cleared = []
    errors = []
    
    if log_type == "all":
        files_to_clear = list(log_files.values())
    elif log_type in log_files:
        files_to_clear = [log_files[log_type]]
    else:
        raise HTTPException(status_code=400, detail=f"Invalid log type: {log_type}. Use: access, security, audit, error, or all")
    
    # Map log file names to DB log_source values
    _file_to_source = {"access.log": "access", "security.log": "security",
                        "audit.log": "audit", "error.log": "error"}

    for log_file in files_to_clear:
        log_path = LOG_DIR / log_file
        try:
            if log_path.exists():
                # Create backup before clearing
                backup_path = LOG_DIR / f"{log_file}.backup"
                import shutil
                shutil.copy2(log_path, backup_path)
                
                # Clear the log file
                with open(log_path, 'w') as f:
                    f.write('')
                
                cleared.append(log_file)

            # Also clear matching DB telemetry
            if db and log_file in _file_to_source:
                telemetry_source = _file_to_source[log_file]

                def _work():
                    db.clear_telemetry(telemetry_source)

                await asyncio.to_thread(_work)

                # Log the clear action (to security log, which might have just been cleared)
                security_logger.log_security_event(
                    event_type="log_cleared",
                    severity="high",
                    client_ip=client_ip,
                    message=f"Log file cleared: {log_file}",
                    request_id=request_id
                )
        except Exception as e:
            errors.append({"file": log_file, "error": str(e)})
    
    return {
        "success": len(errors) == 0,
        "cleared": cleared,
        "errors": errors,
        "message": f"Cleared {len(cleared)} log file(s)" + (f" with {len(errors)} error(s)" if errors else "")
    }


# Global state for date extraction progress
_date_extraction_status = {
    "running": False,
    "total": 0,
    "processed": 0,
    "updated": 0,
    "errors": 0,
    "started_at": None,
    "completed_at": None
}


def run_date_extraction():
    """Background task to extract dates from all documents without a document_date."""
    global _date_extraction_status, db
    
    _date_extraction_status["running"] = True
    _date_extraction_status["started_at"] = datetime.now().isoformat()
    _date_extraction_status["completed_at"] = None
    _date_extraction_status["processed"] = 0
    _date_extraction_status["updated"] = 0
    _date_extraction_status["errors"] = 0
    
    try:
        # Get all documents without a date
        with db.get_connection() as conn:
            cursor = conn.execute("""
                SELECT id, full_text FROM documents 
                WHERE document_date IS NULL AND full_text IS NOT NULL
            """)
            documents = cursor.fetchall()
        
        _date_extraction_status["total"] = len(documents)
        
        # Process in batches of 100
        batch_size = 100
        updates = []
        
        for doc in documents:
            doc_id = doc["id"]
            full_text = doc["full_text"] or ""
            
            try:
                date = extract_email_date(full_text)
                if date:
                    updates.append((date, doc_id))
                    _date_extraction_status["updated"] += 1
            except Exception:
                _date_extraction_status["errors"] += 1
            
            _date_extraction_status["processed"] += 1
            
            # Commit in batches
            if len(updates) >= batch_size:
                with db.get_connection() as conn:
                    conn.executemany(
                        "UPDATE documents SET document_date = ? WHERE id = ?",
                        updates
                    )
                    conn.commit()
                updates = []
        
        # Commit remaining updates
        if updates:
            with db.get_connection() as conn:
                conn.executemany(
                    "UPDATE documents SET document_date = ? WHERE id = ?",
                    updates
                )
                conn.commit()
    
    except Exception as e:
        print(f"Date extraction error: {e}")
        _date_extraction_status["errors"] += 1
    
    finally:
        _date_extraction_status["running"] = False
        _date_extraction_status["completed_at"] = datetime.now().isoformat()


@app.post("/api/admin/extract-dates")
async def extract_document_dates(
    request: Request,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(None)
):
    """
    Run date extraction on all documents without a document_date (requires admin authentication).
    This runs in the background and progress can be monitored via GET /api/admin/extract-dates/status.
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if _date_extraction_status["running"]:
        return {
            "status": "already_running",
            "message": "Date extraction is already in progress",
            "progress": _date_extraction_status
        }
    
    background_tasks.add_task(run_date_extraction)
    
    return {
        "status": "started",
        "message": "Date extraction started in background. Check /api/admin/extract-dates/status for progress."
    }


@app.get("/api/admin/extract-dates/status")
async def get_date_extraction_status(request: Request, x_api_key: str = Header(None)):
    """Get the current status of date extraction (requires admin authentication)."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    return _date_extraction_status


@app.get("/api/admin/telemetry/ai-summaries")
async def get_ai_summaries_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get list of documents that have had AI summaries generated (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    jv = lambda k: _json_val('data', k)

    rows = await _atel(
        f"SELECT {jv('document_id')} AS doc_id, {jv('filename')} AS filename, "
        f"SUM(CASE WHEN {jv('action')}='summary_generate' THEN 1 ELSE 0 END) AS gen_count, "
        f"SUM(CASE WHEN {jv('action')}='summary_cached' THEN 1 ELSE 0 END) AS cache_count, "
        f"MIN(timestamp) AS first_gen, MAX(timestamp) AS last_gen "
        f"FROM telemetry_events WHERE event_type='document_access' "
        f"AND {jv('action')} IN ('summary_generate','summary_cached') "
        f"GROUP BY doc_id ORDER BY last_gen DESC LIMIT 50")

    sorted_docs = [{"document_id": r["doc_id"] or "", "filename": r["filename"] or "Unknown",
                     "generated_count": r["gen_count"], "cached_count": r["cache_count"],
                     "first_generated": r["first_gen"], "last_generated": r["last_gen"]} for r in rows]

    total_gen = sum(d["generated_count"] for d in sorted_docs)
    total_cache = sum(d["cached_count"] for d in sorted_docs)

    return {
        "total_documents_with_summaries": len(sorted_docs),
        "total_generations": total_gen,
        "total_cache_hits": total_cache,
        "documents": sorted_docs
    }


@app.get("/api/admin/telemetry/feedback")
async def get_feedback_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get user feedback submissions (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    feedback_list = []
    if FEEDBACK_PATH.exists():
        try:
            with open(FEEDBACK_PATH, 'r') as f:
                feedback_list = json.load(f)
        except Exception:
            feedback_list = []
    
    # Sort by timestamp (newest first)
    feedback_list = sorted(
        feedback_list,
        key=lambda x: x.get("timestamp", ""),
        reverse=True
    )
    
    # Calculate stats by type
    type_counts = {}
    for fb in feedback_list:
        fb_type = fb.get("type", "unknown")
        type_counts[fb_type] = type_counts.get(fb_type, 0) + 1
    
    # Get recent feedback with limited message preview
    recent_feedback = []
    for fb in feedback_list[:100]:  # Last 100 items
        recent_feedback.append({
            "id": fb.get("id", ""),
            "timestamp": fb.get("timestamp", ""),
            "type": fb.get("type", "unknown"),
            "email": fb.get("email", ""),
            "message": fb.get("message", ""),
            "ip": fb.get("ip", "Unknown"),
            "status": fb.get("status", "new")
        })
    
    return {
        "total_feedback": len(feedback_list),
        "type_counts": type_counts,
        "feedback": recent_feedback
    }


@app.delete("/api/admin/feedback/{feedback_id}")
async def delete_feedback(feedback_id: str, request: Request, x_api_key: str = Header(None)):
    """Delete a specific feedback entry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not FEEDBACK_PATH.exists():
        raise HTTPException(status_code=404, detail="Feedback file not found")
    
    try:
        with open(FEEDBACK_PATH, 'r') as f:
            feedback_list = json.load(f)
        
        # Find and remove the feedback entry
        original_length = len(feedback_list)
        feedback_list = [fb for fb in feedback_list if fb.get("id") != feedback_id]
        
        if len(feedback_list) == original_length:
            raise HTTPException(status_code=404, detail="Feedback entry not found")
        
        # Save updated list
        with open(FEEDBACK_PATH, 'w') as f:
            json.dump(feedback_list, f, indent=2)
        
        return {"message": "Feedback deleted successfully", "id": feedback_id}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting feedback: {str(e)}")


class FeedbackStatusUpdate(BaseModel):
    status: str


class BulkFeedbackStatusUpdate(BaseModel):
    ids: list[str]
    status: str


class BulkFeedbackDelete(BaseModel):
    ids: list[str]


VALID_FEEDBACK_STATUSES = ["new", "read", "in-progress", "completed", "archived"]


@app.patch("/api/admin/feedback/{feedback_id}/status")
async def update_feedback_status(feedback_id: str, status_update: FeedbackStatusUpdate, request: Request, x_api_key: str = Header(None)):
    """Update the status of a feedback entry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if status_update.status not in VALID_FEEDBACK_STATUSES:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status. Must be one of: {', '.join(VALID_FEEDBACK_STATUSES)}"
        )
    
    if not FEEDBACK_PATH.exists():
        raise HTTPException(status_code=404, detail="Feedback file not found")
    
    try:
        with open(FEEDBACK_PATH, 'r') as f:
            feedback_list = json.load(f)
        
        # Find and update the feedback entry
        found = False
        for fb in feedback_list:
            if fb.get("id") == feedback_id:
                fb["status"] = status_update.status
                found = True
                break
        
        if not found:
            raise HTTPException(status_code=404, detail="Feedback entry not found")
        
        # Save updated list
        with open(FEEDBACK_PATH, 'w') as f:
            json.dump(feedback_list, f, indent=2)
        
        return {"message": "Status updated successfully", "id": feedback_id, "status": status_update.status}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating feedback status: {str(e)}")


@app.post("/api/admin/feedback/bulk/status")
async def bulk_update_feedback_status(bulk_update: BulkFeedbackStatusUpdate, request: Request, x_api_key: str = Header(None)):
    """Bulk update status of multiple feedback entries"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if bulk_update.status not in VALID_FEEDBACK_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(VALID_FEEDBACK_STATUSES)}")
    
    if not bulk_update.ids:
        raise HTTPException(status_code=400, detail="No feedback IDs provided")
    
    if not FEEDBACK_PATH.exists():
        raise HTTPException(status_code=404, detail="Feedback file not found")
    
    try:
        with open(FEEDBACK_PATH, 'r') as f:
            feedback_list = json.load(f)
        
        ids_set = set(bulk_update.ids)
        updated = 0
        for fb in feedback_list:
            if fb.get("id") in ids_set:
                fb["status"] = bulk_update.status
                updated += 1
        
        with open(FEEDBACK_PATH, 'w') as f:
            json.dump(feedback_list, f, indent=2)
        
        return {"message": f"Updated {updated} feedback entries", "updated": updated}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error bulk updating feedback: {str(e)}")


@app.post("/api/admin/feedback/bulk/delete")
async def bulk_delete_feedback(bulk_delete: BulkFeedbackDelete, request: Request, x_api_key: str = Header(None)):
    """Bulk delete multiple feedback entries"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not bulk_delete.ids:
        raise HTTPException(status_code=400, detail="No feedback IDs provided")
    
    if not FEEDBACK_PATH.exists():
        raise HTTPException(status_code=404, detail="Feedback file not found")
    
    try:
        with open(FEEDBACK_PATH, 'r') as f:
            feedback_list = json.load(f)
        
        ids_set = set(bulk_delete.ids)
        original_length = len(feedback_list)
        feedback_list = [fb for fb in feedback_list if fb.get("id") not in ids_set]
        deleted = original_length - len(feedback_list)
        
        with open(FEEDBACK_PATH, 'w') as f:
            json.dump(feedback_list, f, indent=2)
        
        return {"message": f"Deleted {deleted} feedback entries", "deleted": deleted}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error bulk deleting feedback: {str(e)}")


# =============================================================================
# SETTINGS API ENDPOINTS
# =============================================================================

@app.get("/api/settings")
async def get_public_settings():
    """Get public settings (no auth required)"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    settings = await asyncio.to_thread(db.get_all_settings)

    # Only expose certain settings to the public
    public_settings = {
        "ask_ai_enabled": settings.get("ask_ai_enabled", "true") == "true",
        "pinned_documents_enabled": settings.get("pinned_documents_enabled", "true") == "true",
        "ads_enabled": settings.get("ads_enabled", "false") == "true",
        "affiliate_enabled": settings.get("affiliate_enabled", "true") == "true",
    }
    
    return public_settings


@app.get("/api/admin/settings")
async def get_admin_settings(request: Request, x_api_key: str = Header(None)):
    """Get all settings (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    return await asyncio.to_thread(db.get_all_settings)


class SettingUpdate(BaseModel):
    key: str
    value: str


@app.post("/api/admin/settings")
async def update_setting(setting: SettingUpdate, request: Request, x_api_key: str = Header(None)):
    """Update a setting (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)
    
    # Log setting change
    security_logger.log_security_event(
        event_type="setting_changed",
        severity="info",
        client_ip=client_ip,
        message=f"Setting '{setting.key}' changed to '{setting.value}'",
        request_id=request_id
    )

    def _work():
        db.set_setting(setting.key, setting.value)

    await asyncio.to_thread(_work)

    return {"success": True, "key": setting.key, "value": setting.value}


# =============================================================================
# STATUS PAGE API ENDPOINTS
# =============================================================================

class StatusPageUpdate(BaseModel):
    enabled: bool
    title: Optional[str] = "Under Maintenance"
    message: Optional[str] = "We're performing scheduled maintenance. Please check back soon."
    timeline: Optional[str] = ""


@app.get("/api/admin/status-page")
async def get_status_page(request: Request, x_api_key: str = Header(None)):
    """Get current status page settings (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return {
            "enabled": db.get_setting("status_page_enabled", "false") == "true",
            "title": db.get_setting("status_page_title", "Under Maintenance"),
            "message": db.get_setting(
                "status_page_message",
                "We're performing scheduled maintenance. Please check back soon.",
            ),
            "timeline": db.get_setting("status_page_timeline", ""),
            "started": db.get_setting("status_page_started", ""),
        }

    data = await asyncio.to_thread(_work)
    data["indexing_active"] = MAINTENANCE_LOCK.exists()
    return data


@app.post("/api/admin/status-page")
async def update_status_page(status: StatusPageUpdate, request: Request, x_api_key: str = Header(None)):
    """Update status page settings (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        db.set_setting("status_page_enabled", "true" if status.enabled else "false")
        db.set_setting("status_page_title", status.title or "Under Maintenance")
        db.set_setting("status_page_message", status.message or "")
        db.set_setting("status_page_timeline", status.timeline or "")
        if status.enabled:
            current_started = db.get_setting("status_page_started", "")
            if not current_started:
                db.set_setting("status_page_started", datetime.now().isoformat())
        else:
            db.set_setting("status_page_started", "")

    await asyncio.to_thread(_work)
    _maintenance_cache.invalidate("status_page_enabled")

    # Log the change
    action = "enabled" if status.enabled else "disabled"
    security_logger.log_security_event(
        event_type="status_page_changed",
        severity="info",
        client_ip=client_ip,
        message=f"Status page {action}: {status.title}",
        request_id=request_id
    )
    
    return {
        "success": True,
        "enabled": status.enabled,
        "title": status.title,
        "message": status.message,
        "timeline": status.timeline
    }


@app.post("/api/admin/status-page/disable")
async def disable_status_page(request: Request, x_api_key: str = Header(None)):
    """Quick disable status page (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        db.set_setting("status_page_enabled", "false")
        db.set_setting("status_page_started", "")

    await asyncio.to_thread(_work)
    _maintenance_cache.invalidate("status_page_enabled")

    security_logger.log_security_event(
        event_type="status_page_disabled",
        severity="info",
        client_ip=client_ip,
        message="Status page disabled",
        request_id=request_id
    )
    
    return {"success": True, "enabled": False}


# =============================================================================
# PINNED DOCUMENTS API ENDPOINTS
# =============================================================================

@app.get("/api/pinned-documents")
async def get_pinned_documents():
    """Get all pinned/featured documents (public endpoint)
    
    Note: Hidden documents are excluded from the pinned list.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Exclude hidden documents from public pinned list
    pinned = await asyncio.to_thread(db.get_pinned_documents, include_hidden=False)
    return {"pinned_documents": pinned}


class PinDocumentRequest(BaseModel):
    document_id: str
    reason: Optional[str] = None
    display_order: int = 0


@app.post("/api/admin/pinned-documents")
async def pin_document(pin_request: PinDocumentRequest, request: Request, x_api_key: str = Header(None)):
    """Pin a document (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        return db.pin_document(
            pin_request.document_id,
            pin_request.reason,
            pin_request.display_order,
        )

    success = await asyncio.to_thread(_work)

    if not success:
        raise HTTPException(status_code=404, detail="Document not found")
    
    security_logger.log_security_event(
        event_type="document_pinned",
        severity="info",
        client_ip=client_ip,
        message=f"Document pinned: {pin_request.document_id}",
        request_id=request_id,
        document_id=pin_request.document_id
    )
    
    return {"success": True, "document_id": pin_request.document_id}


class UpdatePinRequest(BaseModel):
    reason: Optional[str] = None
    display_order: Optional[int] = None


@app.put("/api/admin/pinned-documents/{document_id}")
async def update_pinned_document(
    document_id: str, 
    update_request: UpdatePinRequest, 
    request: Request, 
    x_api_key: str = Header(None)
):
    """Update a pinned document's reason or order (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.update_pinned_document(
            document_id,
            update_request.reason,
            update_request.display_order,
        )

    success = await asyncio.to_thread(_work)

    if not success:
        raise HTTPException(status_code=404, detail="Pinned document not found")
    
    return {"success": True, "document_id": document_id}


@app.delete("/api/admin/pinned-documents/{document_id}")
async def unpin_document(document_id: str, request: Request, x_api_key: str = Header(None)):
    """Unpin a document (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        return db.unpin_document(document_id)

    success = await asyncio.to_thread(_work)

    if not success:
        raise HTTPException(status_code=404, detail="Pinned document not found")

    security_logger.log_security_event(
        event_type="document_unpinned",
        severity="info",
        client_ip=client_ip,
        message=f"Document unpinned: {document_id}",
        request_id=request_id,
        document_id=document_id
    )
    
    return {"success": True, "document_id": document_id}


@app.get("/api/admin/pinned-documents")
async def get_admin_pinned_documents(request: Request, x_api_key: str = Header(None)):
    """Get all pinned documents with admin details (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_pinned_documents(include_hidden=True)

    pinned = await asyncio.to_thread(_work)
    return {"pinned_documents": pinned}


# =============================================================================
# Document Visibility Endpoints (Admin only)
# =============================================================================

@app.get("/api/admin/hidden-documents")
async def get_hidden_documents(
    request: Request, 
    x_api_key: str = Header(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    """Get all hidden documents (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Not cached: this list is reloaded by the admin UI immediately after a hide/unhide and
    # must stay fresh. It is cheap anyway (covering index on a read connection).
    def _work():
        docs = db.get_hidden_documents(limit=limit, offset=offset, timeout_seconds=_ADMIN_QUERY_TIMEOUT)
        total = db.count_hidden_documents(timeout_seconds=_ADMIN_QUERY_TIMEOUT)
        return docs, total

    docs, total = await asyncio.to_thread(_work)

    return {
        "hidden_documents": docs,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.post("/api/admin/documents/{doc_id}/hide")
async def hide_document(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Hide a document from public view (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    try:
        def _work():
            doc = db.get_document(doc_id, include_hidden=True)
            if not doc:
                return None, False
            return doc, db.hide_document(doc_id)

        doc, success = await asyncio.to_thread(_work)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        if success:
            _categories_cache.invalidate()
            _subcategories_cache.invalidate()
            _stats_cache.invalidate()
            _bootstrap_cache.invalidate()

            security_logger.log_system_event(
                "document_hidden",
                f"Document hidden by admin: {doc_id} ({doc.get('filename', 'Unknown')})",
                document_id=doc_id
            )
            return {"success": True, "message": f"Document '{doc.get('filename', doc_id)}' is now hidden"}

        raise HTTPException(status_code=500, detail="Failed to hide document")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error hiding document: {str(e)}")


@app.post("/api/admin/documents/{doc_id}/unhide")
async def unhide_document(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Unhide a document (make visible to public) (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    try:
        def _work():
            doc = db.get_document(doc_id, include_hidden=True)
            if not doc:
                return None, False
            return doc, db.unhide_document(doc_id)

        doc, success = await asyncio.to_thread(_work)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        if success:
            _categories_cache.invalidate()
            _subcategories_cache.invalidate()
            _stats_cache.invalidate()
            _bootstrap_cache.invalidate()

            security_logger.log_system_event(
                "document_unhidden",
                f"Document unhidden by admin: {doc_id} ({doc.get('filename', 'Unknown')})",
                document_id=doc_id
            )
            return {"success": True, "message": f"Document '{doc.get('filename', doc_id)}' is now visible"}

        raise HTTPException(status_code=500, detail="Failed to unhide document")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error unhiding document: {str(e)}")


# =========================================================================
# Document Re-download and Re-extract Endpoints
# =========================================================================

DATASET_EFTA_RANGES = {
    1:  (1, 3158),
    2:  (3159, 3857),
    3:  (3858, 5704),
    4:  (5705, 8408),
    5:  (8409, 8528),
    6:  (8529, 9015),
    7:  (9016, 9675),
    8:  (9676, 39024),
    9:  (39025, 1262781),
    10: (1262782, 2212882),
    11: (2212883, 2730264),
    12: (2730265, 3000000),
}

DOJ_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_17_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept": "application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "justiceGovAgeVerified=true",
}


def _parse_efta_info(filename: str):
    """Parse EFTA number and dataset from a filename like 'EFTA00549159.pdf'.
    Returns (efta_number, dataset_num) or (None, None) if not an EFTA file.
    """
    import re
    match = re.match(r'^EFTA(\d+)', filename, re.IGNORECASE)
    if not match:
        return None, None
    efta_num = int(match.group(1))
    for ds, (start, end) in DATASET_EFTA_RANGES.items():
        if start <= efta_num <= end:
            return efta_num, ds
    return efta_num, None


@app.post("/api/admin/documents/{doc_id}/redownload")
async def redownload_document(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Re-download a document file from the DOJ website (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    try:
        def _work():
            return db.get_document(doc_id, include_hidden=True)

        doc = await asyncio.to_thread(_work)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        filename = doc.get("filename", "")
        efta_num, dataset_num = _parse_efta_info(filename)
        if efta_num is None:
            raise HTTPException(status_code=400, detail=f"Not an EFTA file: {filename}")
        if dataset_num is None:
            raise HTTPException(status_code=400, detail=f"EFTA number {efta_num} outside known dataset ranges")

        ext = Path(filename).suffix or ".pdf"
        doj_url = f"https://www.justice.gov/epstein/files/DataSet%20{dataset_num}/EFTA{efta_num:08d}{ext}"

        file_path = (BASE_PATH / doc["path"]).resolve()
        if not str(file_path).startswith(str(BASE_PATH.resolve())):
            raise HTTPException(status_code=403, detail="Path traversal denied")

        old_size = file_path.stat().st_size if file_path.exists() else 0

        # Back up the original before overwriting
        backup_path = file_path.with_suffix(file_path.suffix + ".bak")
        if file_path.exists():
            import shutil
            shutil.copy2(str(file_path), str(backup_path))

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(doj_url, headers=DOJ_DOWNLOAD_HEADERS)

        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"File not found on DOJ site: {doj_url}")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"DOJ returned HTTP {resp.status_code}")
        if len(resp.content) < 100:
            raise HTTPException(status_code=502, detail="Downloaded file is suspiciously small; possible access block")

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(resp.content)
        new_size = len(resp.content)

        # Remove stale extracted text so re-extract picks up the new content
        extracted_json = BASE_PATH / "extracted_text" / f"{doc_id}.json"
        if extracted_json.exists():
            extracted_json.unlink()

        # Remove stale thumbnail so it regenerates from the new file
        stale_thumb = THUMBNAILS_PATH / f"{doc_id}.jpg"
        if stale_thumb.exists():
            stale_thumb.unlink()

        # Clean up backup on success
        if backup_path.exists():
            backup_path.unlink()

        security_logger.log_system_event(
            "document_redownloaded",
            f"Document re-downloaded from DOJ: {filename} (old={old_size}, new={new_size})",
            document_id=doc_id
        )

        return {
            "success": True,
            "filename": filename,
            "old_size": old_size,
            "new_size": new_size,
            "size_changed": old_size != new_size,
            "doj_url": doj_url,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error re-downloading document: {str(e)}")


@app.post("/api/admin/documents/{doc_id}/re-extract")
async def re_extract_document(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Re-extract text/transcript from a document file (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    try:
        def _fetch_doc():
            return db.get_document(doc_id, include_hidden=True)

        doc = await asyncio.to_thread(_fetch_doc)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        file_path = (BASE_PATH / doc["path"]).resolve()
        if not str(file_path).startswith(str(BASE_PATH.resolve())):
            raise HTTPException(status_code=403, detail="Path traversal denied")
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found on disk: {doc['path']}")

        ext = file_path.suffix.lower()
        pdf_exts = {'.pdf'}
        image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.tiff', '.bmp'}
        media_exts = {'.mp3', '.mp4', '.wav', '.m4a', '.avi', '.mov', '.wmv'}

        from extractor import PDFExtractor, ImageExtractor, AudioVideoExtractor

        result = None
        if ext in pdf_exts:
            extractor = PDFExtractor(str(BASE_PATH))
            result = extractor.extract_pdf(file_path)
        elif ext in image_exts:
            extractor = ImageExtractor(str(BASE_PATH))
            result = extractor.extract_image(file_path)
        elif ext in media_exts:
            extractor = AudioVideoExtractor(str(BASE_PATH))
            result = extractor.transcribe_file(file_path)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

        if not result or not result.get("has_content"):
            error_msg = result.get("error", "No text content extracted") if result else "Extraction returned nothing"
            raise HTTPException(status_code=500, detail=error_msg)

        # Save the extracted JSON
        extracted_dir = BASE_PATH / "extracted_text"
        extracted_dir.mkdir(exist_ok=True)
        output_file = extracted_dir / f"{doc_id}.json"
        with open(output_file, 'w') as f:
            json.dump(result, f)

        # Update the database record via INSERT OR REPLACE
        result["id"] = doc_id
        result["path"] = doc["path"]
        result["is_hidden"] = doc.get("is_hidden", 0)

        def _persist():
            db.insert_document(result)
            vector_updated = False
            full_text = result.get("full_text", "")
            if vector_store and full_text and len(full_text) > 100:
                try:
                    vector_store.add_document(
                        doc_id=doc_id,
                        text=full_text,
                        metadata={
                            "filename": doc.get("filename", ""),
                            "category": result.get("category", doc.get("category", "Unknown")),
                        },
                    )
                    vector_store._save()
                    vector_updated = True
                except Exception as ve:
                    print(f"Warning: Failed to update vector store for {doc_id}: {ve}")
            return vector_updated

        vector_updated = await asyncio.to_thread(_persist)

        # Remove stale thumbnail so it regenerates from the updated content
        stale_thumb = THUMBNAILS_PATH / f"{doc_id}.jpg"
        if stale_thumb.exists():
            stale_thumb.unlink()

        # Invalidate caches
        _subcategories_cache.invalidate()
        _stats_cache.invalidate()
        _bootstrap_cache.invalidate()

        security_logger.log_system_event(
            "document_re_extracted",
            f"Document text re-extracted: {doc.get('filename', doc_id)} (chars={result.get('char_count', 0)}, pages={result.get('page_count', 0)}, vector={'updated' if vector_updated else 'skipped'})",
            document_id=doc_id
        )

        return {
            "success": True,
            "filename": doc.get("filename", ""),
            "char_count": result.get("char_count", 0),
            "page_count": result.get("page_count", 0),
            "file_type": result.get("file_type", ext.lstrip('.')),
            "has_content": True,
            "vector_updated": vector_updated,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error re-extracting document: {str(e)}")


# =========================================================================
# Bulk Document Re-download / Re-extract Endpoints
# =========================================================================

class BulkFilenamesRequest(BaseModel):
    filenames: List[str]


@app.post("/api/admin/documents/resolve-filenames")
async def resolve_filenames(body: BulkFilenamesRequest, request: Request, x_api_key: str = Header(None)):
    """Resolve a list of filenames to document records (for bulk operations UI)."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    filenames = [f.strip() for f in body.filenames if f.strip()]
    if not filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    if len(filenames) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 filenames")

    def _work():
        return db.get_documents_by_filenames(filenames)

    doc_map = await asyncio.to_thread(_work)
    not_found = [f for f in filenames if f not in doc_map]

    return {
        "found": doc_map,
        "not_found": not_found,
    }


# =========================================================================
# Background Task Infrastructure (async ops behind Cloudflare proxy)
# =========================================================================

_bg_tasks: dict = {}
_BG_TASK_TTL = 3600


def _cleanup_bg_tasks():
    now = _time.time()
    expired = [tid for tid, t in _bg_tasks.items()
               if t["status"] != "running" and now - t.get("created_at", 0) > _BG_TASK_TTL]
    for tid in expired:
        del _bg_tasks[tid]


async def _bg_redownload(task_id: str, doc_id: str):
    try:
        def _fetch():
            return db.get_document(doc_id, include_hidden=True)

        doc = await asyncio.to_thread(_fetch)
        if not doc:
            _bg_tasks[task_id].update(status="failed", result={"error": "Document not found"})
            return

        filename = doc.get("filename", "")
        efta_num, dataset_num = _parse_efta_info(filename)
        if efta_num is None:
            _bg_tasks[task_id].update(status="failed", result={"error": f"Not an EFTA file: {filename}"})
            return
        if dataset_num is None:
            _bg_tasks[task_id].update(status="failed", result={"error": f"EFTA number outside known dataset ranges"})
            return

        ext = Path(filename).suffix or ".pdf"
        doj_url = f"https://www.justice.gov/epstein/files/DataSet%20{dataset_num}/EFTA{efta_num:08d}{ext}"
        file_path = (BASE_PATH / doc["path"]).resolve()
        if not str(file_path).startswith(str(BASE_PATH.resolve())):
            _bg_tasks[task_id].update(status="failed", result={"error": "Path traversal denied"})
            return

        old_size = file_path.stat().st_size if file_path.exists() else 0

        import shutil
        backup_path = file_path.with_suffix(file_path.suffix + ".bak")
        if file_path.exists():
            shutil.copy2(str(file_path), str(backup_path))

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(doj_url, headers=DOJ_DOWNLOAD_HEADERS)

        if resp.status_code == 404:
            if backup_path.exists():
                backup_path.unlink()
            _bg_tasks[task_id].update(status="failed", result={"error": f"Not found on DOJ: {doj_url}"})
            return
        if resp.status_code != 200:
            if backup_path.exists():
                backup_path.unlink()
            _bg_tasks[task_id].update(status="failed", result={"error": f"DOJ returned HTTP {resp.status_code}"})
            return
        if len(resp.content) < 100:
            if backup_path.exists():
                backup_path.unlink()
            _bg_tasks[task_id].update(status="failed", result={"error": "Suspiciously small file from DOJ"})
            return

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(resp.content)
        new_size = len(resp.content)

        extracted_json = BASE_PATH / "extracted_text" / f"{doc_id}.json"
        if extracted_json.exists():
            extracted_json.unlink()
        stale_thumb = THUMBNAILS_PATH / f"{doc_id}.jpg"
        if stale_thumb.exists():
            stale_thumb.unlink()
        if backup_path.exists():
            backup_path.unlink()

        _subcategories_cache.invalidate()
        _stats_cache.invalidate()
        _bootstrap_cache.invalidate()

        security_logger.log_system_event(
            "document_redownloaded",
            f"Document re-downloaded from DOJ (async): {filename} (old={old_size}, new={new_size})",
            document_id=doc_id
        )

        _bg_tasks[task_id].update(status="completed", result={
            "success": True,
            "filename": filename,
            "old_size": old_size,
            "new_size": new_size,
            "size_changed": old_size != new_size,
            "doj_url": doj_url,
        })
    except Exception as e:
        _bg_tasks[task_id].update(status="failed", result={"error": str(e)})


async def _bg_re_extract(task_id: str, doc_id: str):
    try:
        def _fetch():
            return db.get_document(doc_id, include_hidden=True)

        doc = await asyncio.to_thread(_fetch)
        if not doc:
            _bg_tasks[task_id].update(status="failed", result={"error": "Document not found"})
            return

        file_path = (BASE_PATH / doc["path"]).resolve()
        if not str(file_path).startswith(str(BASE_PATH.resolve())):
            _bg_tasks[task_id].update(status="failed", result={"error": "Path traversal denied"})
            return
        if not file_path.exists():
            _bg_tasks[task_id].update(status="failed", result={"error": f"File not found on disk: {doc['path']}"})
            return

        ext = file_path.suffix.lower()
        pdf_exts = {'.pdf'}
        image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.tiff', '.bmp'}
        media_exts = {'.mp3', '.mp4', '.wav', '.m4a', '.avi', '.mov', '.wmv'}

        from extractor import PDFExtractor, ImageExtractor, AudioVideoExtractor

        result = None
        if ext in pdf_exts:
            extractor = PDFExtractor(str(BASE_PATH))
            result = extractor.extract_pdf(file_path)
        elif ext in image_exts:
            extractor = ImageExtractor(str(BASE_PATH))
            result = extractor.extract_image(file_path)
        elif ext in media_exts:
            extractor = AudioVideoExtractor(str(BASE_PATH))
            result = extractor.transcribe_file(file_path)
        else:
            _bg_tasks[task_id].update(status="failed", result={"error": f"Unsupported file type: {ext}"})
            return

        if not result or not result.get("has_content"):
            error_msg = result.get("error", "No text content extracted") if result else "Extraction returned nothing"
            _bg_tasks[task_id].update(status="failed", result={"error": error_msg})
            return

        extracted_dir = BASE_PATH / "extracted_text"
        extracted_dir.mkdir(exist_ok=True)
        output_file = extracted_dir / f"{doc_id}.json"
        with open(output_file, 'w') as f:
            json.dump(result, f)

        result["id"] = doc_id
        result["path"] = doc["path"]
        result["is_hidden"] = doc.get("is_hidden", 0)

        def _persist():
            db.insert_document(result)
            vector_updated = False
            full_text = result.get("full_text", "")
            if vector_store and full_text and len(full_text) > 100:
                try:
                    vector_store.add_document(
                        doc_id=doc_id,
                        text=full_text,
                        metadata={
                            "filename": doc.get("filename", ""),
                            "category": result.get("category", doc.get("category", "Unknown")),
                        },
                    )
                    vector_store._save()
                    vector_updated = True
                except Exception:
                    pass
            return vector_updated

        vector_updated = await asyncio.to_thread(_persist)

        stale_thumb = THUMBNAILS_PATH / f"{doc_id}.jpg"
        if stale_thumb.exists():
            stale_thumb.unlink()

        _subcategories_cache.invalidate()
        _stats_cache.invalidate()
        _bootstrap_cache.invalidate()

        security_logger.log_system_event(
            "document_re_extracted",
            f"Document text re-extracted (async): {doc.get('filename', doc_id)} "
            f"(chars={result.get('char_count', 0)}, pages={result.get('page_count', 0)})",
            document_id=doc_id
        )

        _bg_tasks[task_id].update(status="completed", result={
            "success": True,
            "filename": doc.get("filename", ""),
            "char_count": result.get("char_count", 0),
            "page_count": result.get("page_count", 0),
            "file_type": result.get("file_type", ext.lstrip('.')),
            "has_content": True,
            "vector_updated": vector_updated,
        })
    except Exception as e:
        _bg_tasks[task_id].update(status="failed", result={"error": str(e)})


@app.post("/api/admin/documents/{doc_id}/redownload-async")
async def redownload_document_async(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Start re-download as a background task, returns task_id for polling."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_document(doc_id, include_hidden=True)

    doc = await asyncio.to_thread(_work)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    _cleanup_bg_tasks()
    task_id = str(uuid.uuid4())
    _bg_tasks[task_id] = {"status": "running", "result": None, "created_at": _time.time()}
    asyncio.create_task(_bg_redownload(task_id, doc_id))
    return {"task_id": task_id, "status": "running"}


@app.post("/api/admin/documents/{doc_id}/re-extract-async")
async def re_extract_document_async(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Start re-extraction as a background task, returns task_id for polling."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_document(doc_id, include_hidden=True)

    doc = await asyncio.to_thread(_work)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    _cleanup_bg_tasks()
    task_id = str(uuid.uuid4())
    _bg_tasks[task_id] = {"status": "running", "result": None, "created_at": _time.time()}
    asyncio.create_task(_bg_re_extract(task_id, doc_id))
    return {"task_id": task_id, "status": "running"}


@app.get("/api/admin/tasks/{task_id}")
async def get_task_status(task_id: str, request: Request, x_api_key: str = Header(None)):
    """Poll the status of a background task."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    task = _bg_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "status": task["status"], "result": task["result"]}


@app.post("/api/admin/documents/bulk-redownload")
async def bulk_redownload_documents(body: BulkFilenamesRequest, request: Request, x_api_key: str = Header(None)):
    """Re-download multiple EFTA documents from the DOJ website in sequence."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    filenames = [f.strip() for f in body.filenames if f.strip()]
    if not filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    if len(filenames) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 filenames per bulk redownload")

    def _work():
        return db.get_documents_by_filenames(filenames)

    doc_map = await asyncio.to_thread(_work)
    not_found = [f for f in filenames if f not in doc_map]

    results = []
    skipped = []
    processed = 0
    failed = 0

    for fname in filenames:
        if fname not in doc_map:
            continue
        doc = doc_map[fname]
        filename = doc.get("filename", fname)

        efta_num, dataset_num = _parse_efta_info(filename)
        if efta_num is None or dataset_num is None:
            skipped.append(filename)
            continue

        ext = Path(filename).suffix or ".pdf"
        doj_url = f"https://www.justice.gov/epstein/files/DataSet%20{dataset_num}/EFTA{efta_num:08d}{ext}"
        doc_id = doc["id"]

        try:
            file_path = (BASE_PATH / doc["path"]).resolve()
            if not str(file_path).startswith(str(BASE_PATH.resolve())):
                results.append({"filename": filename, "success": False, "error": "Path traversal denied"})
                failed += 1
                continue

            old_size = file_path.stat().st_size if file_path.exists() else 0

            import shutil
            backup_path = file_path.with_suffix(file_path.suffix + ".bak")
            if file_path.exists():
                shutil.copy2(str(file_path), str(backup_path))

            async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
                resp = await client.get(doj_url, headers=DOJ_DOWNLOAD_HEADERS)

            if resp.status_code == 404:
                if backup_path.exists():
                    backup_path.unlink()
                results.append({"filename": filename, "success": False, "error": f"Not found on DOJ ({doj_url})"})
                failed += 1
                continue
            if resp.status_code != 200:
                if backup_path.exists():
                    backup_path.unlink()
                results.append({"filename": filename, "success": False, "error": f"DOJ HTTP {resp.status_code}"})
                failed += 1
                continue
            if len(resp.content) < 100:
                if backup_path.exists():
                    backup_path.unlink()
                results.append({"filename": filename, "success": False, "error": "Suspiciously small file from DOJ"})
                failed += 1
                continue

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(resp.content)
            new_size = len(resp.content)

            extracted_json = BASE_PATH / "extracted_text" / f"{doc_id}.json"
            if extracted_json.exists():
                extracted_json.unlink()
            stale_thumb = THUMBNAILS_PATH / f"{doc_id}.jpg"
            if stale_thumb.exists():
                stale_thumb.unlink()
            if backup_path.exists():
                backup_path.unlink()

            processed += 1
            results.append({
                "filename": filename,
                "success": True,
                "old_size": old_size,
                "new_size": new_size,
                "size_changed": old_size != new_size,
            })
        except Exception as e:
            failed += 1
            results.append({"filename": filename, "success": False, "error": str(e)})

    _subcategories_cache.invalidate()
    _stats_cache.invalidate()
    _bootstrap_cache.invalidate()

    security_logger.log_system_event(
        "bulk_documents_redownloaded",
        f"Bulk redownload: {processed} succeeded, {failed} failed, "
        f"{len(skipped)} skipped (non-EFTA), {len(not_found)} not found, "
        f"{len(filenames)} total submitted"
    )

    return {
        "success": True,
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
        "not_found": not_found,
        "results": results,
    }


@app.post("/api/admin/documents/bulk-re-extract")
async def bulk_re_extract_documents(body: BulkFilenamesRequest, request: Request, x_api_key: str = Header(None)):
    """Re-extract text/transcript from multiple documents in sequence."""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    filenames = [f.strip() for f in body.filenames if f.strip()]
    if not filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    if len(filenames) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 filenames per bulk re-extract")

    def _resolve():
        return db.get_documents_by_filenames(filenames)

    doc_map = await asyncio.to_thread(_resolve)
    not_found = [f for f in filenames if f not in doc_map]

    from extractor import PDFExtractor, ImageExtractor, AudioVideoExtractor

    pdf_exts = {'.pdf'}
    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.tiff', '.bmp'}
    media_exts = {'.mp3', '.mp4', '.wav', '.m4a', '.avi', '.mov', '.wmv'}

    results = []
    skipped = []
    processed = 0
    failed = 0

    for fname in filenames:
        if fname not in doc_map:
            continue
        doc = doc_map[fname]
        doc_id = doc["id"]
        filename = doc.get("filename", fname)

        try:
            file_path = (BASE_PATH / doc["path"]).resolve()
            if not str(file_path).startswith(str(BASE_PATH.resolve())):
                results.append({"filename": filename, "success": False, "error": "Path traversal denied"})
                failed += 1
                continue
            if not file_path.exists():
                results.append({"filename": filename, "success": False, "error": "File not found on disk"})
                failed += 1
                continue

            ext = file_path.suffix.lower()
            result = None
            if ext in pdf_exts:
                extractor = PDFExtractor(str(BASE_PATH))
                result = extractor.extract_pdf(file_path)
            elif ext in image_exts:
                extractor = ImageExtractor(str(BASE_PATH))
                result = extractor.extract_image(file_path)
            elif ext in media_exts:
                extractor = AudioVideoExtractor(str(BASE_PATH))
                result = extractor.transcribe_file(file_path)
            else:
                skipped.append(filename)
                continue

            if not result or not result.get("has_content"):
                error_msg = result.get("error", "No text content extracted") if result else "Extraction returned nothing"
                results.append({"filename": filename, "success": False, "error": error_msg})
                failed += 1
                continue

            extracted_dir = BASE_PATH / "extracted_text"
            extracted_dir.mkdir(exist_ok=True)
            output_file = extracted_dir / f"{doc_id}.json"
            with open(output_file, 'w') as f:
                json.dump(result, f)

            result["id"] = doc_id
            result["path"] = doc["path"]
            result["is_hidden"] = doc.get("is_hidden", 0)

            def _persist():
                db.insert_document(result)
                vector_updated = False
                full_text = result.get("full_text", "")
                if vector_store and full_text and len(full_text) > 100:
                    try:
                        vector_store.add_document(
                            doc_id=doc_id,
                            text=full_text,
                            metadata={
                                "filename": filename,
                                "category": result.get("category", doc.get("category", "Unknown")),
                            },
                        )
                        vector_store._save()
                        vector_updated = True
                    except Exception:
                        pass
                return vector_updated

            vector_updated = await asyncio.to_thread(_persist)

            stale_thumb = THUMBNAILS_PATH / f"{doc_id}.jpg"
            if stale_thumb.exists():
                stale_thumb.unlink()

            processed += 1
            results.append({
                "filename": filename,
                "success": True,
                "char_count": result.get("char_count", 0),
                "page_count": result.get("page_count", 0),
                "vector_updated": vector_updated,
            })
        except Exception as e:
            failed += 1
            results.append({"filename": filename, "success": False, "error": str(e)})

    _subcategories_cache.invalidate()
    _stats_cache.invalidate()
    _bootstrap_cache.invalidate()

    security_logger.log_system_event(
        "bulk_documents_re_extracted",
        f"Bulk re-extract: {processed} succeeded, {failed} failed, "
        f"{len(skipped)} skipped (unsupported type), {len(not_found)} not found, "
        f"{len(filenames)} total submitted"
    )

    return {
        "success": True,
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
        "not_found": not_found,
        "results": results,
    }


# =========================================================================
# Bulk Document Visibility Endpoints
# =========================================================================

class BulkHideRequest(BaseModel):
    document_ids: List[str]

class BulkHideByPatternRequest(BaseModel):
    filename_pattern: str

class BulkHideByFilenamesRequest(BaseModel):
    filenames: List[str]
    action: str = "hide"  # "hide" or "unhide"


@app.post("/api/admin/documents/bulk-hide")
async def bulk_hide_documents(body: BulkHideRequest, request: Request, x_api_key: str = Header(None)):
    """Hide multiple documents at once (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    if not body.document_ids:
        raise HTTPException(status_code=400, detail="No document IDs provided")
    
    if len(body.document_ids) > 1000:
        raise HTTPException(status_code=400, detail="Maximum 1000 documents per bulk operation")

    def _work():
        return db.bulk_hide_documents(body.document_ids)

    hidden_count = await asyncio.to_thread(_work)

    # Invalidate all caches
    _categories_cache.invalidate()
    _subcategories_cache.invalidate()
    _stats_cache.invalidate()
    _bootstrap_cache.invalidate()
    
    security_logger.log_system_event(
        "bulk_documents_hidden",
        f"Bulk hide: {hidden_count} documents hidden out of {len(body.document_ids)} requested"
    )
    
    return {
        "success": True,
        "hidden_count": hidden_count,
        "requested_count": len(body.document_ids),
        "message": f"Successfully hidden {hidden_count} document(s)"
    }


@app.post("/api/admin/documents/bulk-unhide")
async def bulk_unhide_documents(body: BulkHideRequest, request: Request, x_api_key: str = Header(None)):
    """Unhide multiple documents at once (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    if not body.document_ids:
        raise HTTPException(status_code=400, detail="No document IDs provided")
    
    if len(body.document_ids) > 1000:
        raise HTTPException(status_code=400, detail="Maximum 1000 documents per bulk operation")

    def _work():
        return db.bulk_unhide_documents(body.document_ids)

    unhidden_count = await asyncio.to_thread(_work)

    # Invalidate all caches
    _categories_cache.invalidate()
    _subcategories_cache.invalidate()
    _stats_cache.invalidate()
    _bootstrap_cache.invalidate()
    
    security_logger.log_system_event(
        "bulk_documents_unhidden",
        f"Bulk unhide: {unhidden_count} documents unhidden out of {len(body.document_ids)} requested"
    )
    
    return {
        "success": True,
        "unhidden_count": unhidden_count,
        "requested_count": len(body.document_ids),
        "message": f"Successfully unhidden {unhidden_count} document(s)"
    }


@app.post("/api/admin/documents/bulk-hide-by-pattern")
async def bulk_hide_by_pattern(body: BulkHideByPatternRequest, request: Request, x_api_key: str = Header(None)):
    """Hide all documents matching a filename pattern (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    if not body.filename_pattern or not body.filename_pattern.strip():
        raise HTTPException(status_code=400, detail="Filename pattern is required")
    
    # Convert user-friendly wildcard (*) to SQL LIKE pattern (%)
    pattern = body.filename_pattern.strip().replace('*', '%')
    # Ensure there is at least one wildcard or it's a specific filename
    if '%' not in pattern:
        pattern = f"%{pattern}%"

    def _work():
        return db.hide_documents_by_filename_pattern(pattern)

    hidden_count = await asyncio.to_thread(_work)

    # Invalidate all caches
    _categories_cache.invalidate()
    _subcategories_cache.invalidate()
    _stats_cache.invalidate()
    _bootstrap_cache.invalidate()
    
    security_logger.log_system_event(
        "bulk_documents_hidden_by_pattern",
        f"Bulk hide by pattern '{body.filename_pattern}': {hidden_count} documents hidden"
    )
    
    return {
        "success": True,
        "hidden_count": hidden_count,
        "pattern": body.filename_pattern,
        "message": f"Successfully hidden {hidden_count} document(s) matching '{body.filename_pattern}'"
    }


@app.post("/api/admin/documents/bulk-hide-by-filenames")
async def bulk_hide_by_filenames(body: BulkHideByFilenamesRequest, request: Request, x_api_key: str = Header(None)):
    """Hide or unhide documents by filename list (CSV paste/upload support)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    if not body.filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    
    if len(body.filenames) > 5000:
        raise HTTPException(status_code=400, detail="Maximum 5000 filenames per bulk operation")
    
    if body.action not in ("hide", "unhide"):
        raise HTTPException(status_code=400, detail="Action must be 'hide' or 'unhide'")

    def _work():
        if body.action == "hide":
            return db.bulk_hide_by_filenames(body.filenames), "hidden_count", "bulk_documents_hidden_by_filenames"
        return db.bulk_unhide_by_filenames(body.filenames), "unhidden_count", "bulk_documents_unhidden_by_filenames"

    result, count_key, event_type = await asyncio.to_thread(_work)

    affected_count = result.get(count_key, 0)
    
    # Invalidate all caches
    _categories_cache.invalidate()
    _subcategories_cache.invalidate()
    _stats_cache.invalidate()
    _bootstrap_cache.invalidate()
    
    security_logger.log_system_event(
        event_type,
        f"Bulk {body.action} by filenames: {affected_count} documents affected, "
        f"{len(result.get('not_found', []))} not found, "
        f"{len(body.filenames)} total submitted"
    )
    
    return {
        "success": True,
        "action": body.action,
        **result,
        "submitted_count": len(body.filenames),
        "message": f"Successfully {'hidden' if body.action == 'hide' else 'unhidden'} {affected_count} document(s)"
    }


@app.get("/api/admin/hidden-categories")
async def get_hidden_categories(request: Request, x_api_key: str = Header(None)):
    """Get all hidden categories (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_hidden_categories()

    hidden = await asyncio.to_thread(_work)
    return {"hidden_categories": hidden}


@app.get("/api/admin/categories-visibility")
async def get_categories_visibility(request: Request, x_api_key: str = Header(None)):
    """Get all categories with their visibility status (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    cached = _admin_cache.get("categories_visibility")
    if cached:
        return cached

    def _work():
        return db.get_all_categories_with_visibility(timeout_seconds=_ADMIN_QUERY_TIMEOUT)

    categories = await asyncio.to_thread(_work)
    result = {"categories": categories}
    _admin_cache.set("categories_visibility", result)
    return result


@app.post("/api/admin/categories/{category}/hide")
async def hide_category(category: str, request: Request, x_api_key: str = Header(None)):
    """Hide an entire category from public view (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # URL decode the category name (handles spaces and special characters)
    from urllib.parse import unquote
    category = unquote(category)

    def _work():
        category_counts = db.get_category_counts(include_hidden=True)
        if not any(c["category"] == category for c in category_counts):
            return "missing", False
        return "ok", db.hide_category(category)

    status, success = await asyncio.to_thread(_work)
    if status == "missing":
        raise HTTPException(status_code=404, detail=f"Category '{category}' not found")

    if success:
        # Invalidate caches
        _categories_cache.invalidate()
        _subcategories_cache.invalidate()
        _stats_cache.invalidate()
        _bootstrap_cache.invalidate()
        _admin_cache.invalidate()

        security_logger.log_system_event(
            "category_hidden",
            f"Category hidden by admin: {category}",
            category=category
        )
        return {"success": True, "message": f"Category '{category}' is now hidden"}
    
    raise HTTPException(status_code=500, detail="Failed to hide category")


@app.post("/api/admin/categories/{category}/unhide")
async def unhide_category(category: str, request: Request, x_api_key: str = Header(None)):
    """Unhide a category (make visible to public) (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # URL decode the category name
    from urllib.parse import unquote
    category = unquote(category)

    def _work():
        return db.unhide_category(category)

    success = await asyncio.to_thread(_work)
    if success:
        # Invalidate caches
        _categories_cache.invalidate()
        _subcategories_cache.invalidate()
        _stats_cache.invalidate()
        _bootstrap_cache.invalidate()
        _admin_cache.invalidate()

        security_logger.log_system_event(
            "category_unhidden",
            f"Category unhidden by admin: {category}",
            category=category
        )
        return {"success": True, "message": f"Category '{category}' is now visible"}
    
    raise HTTPException(status_code=500, detail="Failed to unhide category")


@app.get("/api/admin/documents-visibility")
async def search_documents_visibility(
    request: Request,
    x_api_key: str = Header(None),
    search: Optional[str] = None,
    category: Optional[str] = None,
    file_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """Search documents for visibility management (admin only, includes hidden docs)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    try:
        def _work():
            docs = db.get_all_documents(
                limit=limit,
                offset=offset,
                category=category,
                file_type=file_type,
                search=search,
                include_hidden=True,
            )
            total = db.count_documents(category=category, include_hidden=True)
            return docs, total

        docs, total = await asyncio.to_thread(_work)

        return {
            "documents": docs,
            "total": total,
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching documents: {str(e)}")


class ReclassifyRequest(BaseModel):
    document_ids: List[str]
    file_type: str


@app.patch("/api/admin/documents/reclassify")
async def reclassify_documents(
    body: ReclassifyRequest,
    request: Request,
    x_api_key: str = Header(None),
):
    """Reclassify documents to a new file_type (admin only)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)

    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if body.file_type not in db.VALID_FILE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file_type '{body.file_type}'. Must be one of: {', '.join(sorted(db.VALID_FILE_TYPES))}",
        )

    if not body.document_ids:
        raise HTTPException(status_code=400, detail="No document_ids provided")

    try:
        def _work():
            return db.update_file_type(body.document_ids, body.file_type)

        updated = await asyncio.to_thread(_work)
        return {"success": True, "updated_count": updated}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reclassifying documents: {str(e)}")


# =============================================================================
# Keywords Endpoints
# =============================================================================

@app.get("/api/keywords")
async def get_keywords():
    """Get all active keywords grouped by category (public endpoint)"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    keywords = await asyncio.to_thread(db.get_keywords, active_only=True)

    # Group by category
    grouped = {}
    for kw in keywords:
        category = kw["category"]
        if category not in grouped:
            grouped[category] = []
        grouped[category].append({
            "name": kw["name"],
            "search_term": kw["search_term"],
            "document_count": kw["document_count"]
        })
    
    return {"keywords": grouped}


@app.get("/api/admin/keywords")
async def get_admin_keywords(request: Request, x_api_key: str = Header(None)):
    """Get all keywords with admin details (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_keywords(active_only=False)

    keywords = await asyncio.to_thread(_work)
    return {"keywords": keywords}


class AddKeywordRequest(BaseModel):
    name: str
    search_term: str
    category: str
    display_order: int = 0
    is_active: bool = True


@app.post("/api/admin/keywords")
async def add_keyword(keyword_request: AddKeywordRequest, request: Request, x_api_key: str = Header(None)):
    """Add a new keyword (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        return db.add_keyword(
            name=keyword_request.name,
            search_term=keyword_request.search_term,
            category=keyword_request.category,
            display_order=keyword_request.display_order,
            is_active=keyword_request.is_active,
        )

    keyword_id = await asyncio.to_thread(_work)

    if keyword_id is None:
        raise HTTPException(status_code=400, detail="Keyword with this name already exists")
    
    # Log the action
    security_logger.log_admin_action(
        client_ip=client_ip,
        request_id=request_id,
        action="add_keyword",
        target=keyword_request.name,
        details=f"Added keyword: {keyword_request.name} ({keyword_request.category})"
    )
    
    return {"success": True, "keyword_id": keyword_id}


class UpdateKeywordRequest(BaseModel):
    name: Optional[str] = None
    search_term: Optional[str] = None
    category: Optional[str] = None
    display_order: Optional[int] = None
    is_active: Optional[bool] = None


@app.put("/api/admin/keywords/{keyword_id}")
async def update_keyword(
    keyword_id: int,
    update_request: UpdateKeywordRequest,
    request: Request,
    x_api_key: str = Header(None)
):
    """Update a keyword (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        return db.update_keyword(
            keyword_id=keyword_id,
            name=update_request.name,
            search_term=update_request.search_term,
            category=update_request.category,
            display_order=update_request.display_order,
            is_active=update_request.is_active,
        )

    success = await asyncio.to_thread(_work)

    if not success:
        raise HTTPException(status_code=404, detail="Keyword not found or duplicate name")
    
    # Log the action
    security_logger.log_admin_action(
        client_ip=client_ip,
        request_id=request_id,
        action="update_keyword",
        target=str(keyword_id),
        details=f"Updated keyword ID {keyword_id}"
    )
    
    return {"success": True, "keyword_id": keyword_id}


@app.delete("/api/admin/keywords/{keyword_id}")
async def delete_keyword(keyword_id: int, request: Request, x_api_key: str = Header(None)):
    """Delete a keyword (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        keyword = db.get_keyword(keyword_id)
        if not keyword:
            return None, False
        return keyword, db.delete_keyword(keyword_id)

    keyword, success = await asyncio.to_thread(_work)
    keyword_name = keyword["name"] if keyword else f"ID {keyword_id}"

    if not success:
        raise HTTPException(status_code=404, detail="Keyword not found")
    
    # Log the action
    security_logger.log_admin_action(
        client_ip=client_ip,
        request_id=request_id,
        action="delete_keyword",
        target=keyword_name,
        details=f"Deleted keyword: {keyword_name}"
    )
    
    return {"success": True, "keyword_id": keyword_id}


@app.post("/api/admin/keywords/recount")
async def recount_keywords(request: Request, x_api_key: str = Header(None)):
    """Recount document matches for all keywords (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        return db.update_keyword_counts()

    counts = await asyncio.to_thread(_work)

    # Log the action
    security_logger.log_admin_action(
        client_ip=client_ip,
        request_id=request_id,
        action="recount_keywords",
        target="all",
        details=f"Recounted {len(counts)} keywords"
    )
    
    return {"success": True, "counts": counts}


@app.post("/api/admin/keywords/seed")
async def seed_keywords(request: Request, x_api_key: str = Header(None)):
    """Seed default keywords if none exist (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        return db.seed_default_keywords()

    count = await asyncio.to_thread(_work)

    if count > 0:
        # Log the action
        security_logger.log_admin_action(
            client_ip=client_ip,
            request_id=request_id,
            action="seed_keywords",
            target="default",
            details=f"Seeded {count} default keywords"
        )
    
    return {"success": True, "keywords_added": count}


# =============================================================================
# DOJ Completeness & Missing Documents API (Admin Only)
# =============================================================================

@app.get("/api/admin/doj-completeness")
async def get_doj_completeness(request: Request, x_api_key: str = Header(None)):
    """Get DOJ download completeness statistics (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    cached = _admin_cache.get("doj_completeness")
    if cached:
        return cached

    def _work():
        return (db.get_manifest_stats(timeout_seconds=_ADMIN_QUERY_TIMEOUT),
                db.get_missing_documents_stats(timeout_seconds=_ADMIN_QUERY_TIMEOUT),
                db.get_dataset_db_counts(timeout_seconds=_ADMIN_QUERY_TIMEOUT),
                db.get_updated_documents_counts(timeout_seconds=_ADMIN_QUERY_TIMEOUT))

    manifest_stats, missing_stats, db_counts, updated_counts = await asyncio.to_thread(_work)

    result = {
        "manifest": manifest_stats,
        "missing": missing_stats,
        # Authoritative per-dataset counts from the documents table (all 12 DS).
        "by_dataset_db": db_counts,
        # Per-dataset count of files that have >=1 newer iteration (archived versions).
        "updated_by_dataset": updated_counts,
    }
    _admin_cache.set("doj_completeness", result)
    return result


@app.get("/api/admin/updated-documents")
async def get_updated_documents_route(
    request: Request,
    x_api_key: str = Header(None),
    dataset: int = None
):
    """List EFTA files that have been re-issued (have >=1 archived version).

    Each entry has the canonical (newest) row plus the older archived versions, so
    the admin UI can compare the first version against later iterations. Optionally
    filtered by dataset. Requires admin authentication.
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    cache_key = f"updated_documents:{dataset}"
    cached = _admin_cache.get(cache_key)
    if cached:
        return cached

    def _work():
        return db.get_updated_documents(dataset_num=dataset,
                                        timeout_seconds=_ADMIN_QUERY_TIMEOUT)

    updated = await asyncio.to_thread(_work)
    result = {"updated_documents": updated, "total": len(updated)}
    _admin_cache.set(cache_key, result)
    return result


@app.get("/api/admin/version-diff")
async def get_version_diff(
    request: Request,
    x_api_key: str = Header(None),
    old: str = None,
    new: str = None
):
    """Unified text diff between two document versions (by doc id).

    Computes the diff server-side from the stored full_text of each version so the
    admin can see exactly what changed between an earlier iteration ('old') and the
    current/later one ('new'). Returns structured diff lines for rendering.
    Requires admin authentication.
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    if not old or not new:
        raise HTTPException(status_code=400, detail="old and new doc ids are required")

    cache_key = f"version_diff:{old}:{new}"
    cached = _admin_cache.get(cache_key)
    if cached:
        return cached

    def _work():
        return (db.get_document_full_text(old, include_hidden=True),
                db.get_document_full_text(new, include_hidden=True))

    old_text, new_text = await asyncio.to_thread(_work)
    if old_text is None and new_text is None:
        raise HTTPException(status_code=404, detail="Documents not found")

    import difflib
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    lines = []
    added = removed = 0
    # n=3 lines of context keeps the payload bounded to the changed regions.
    for ln in difflib.unified_diff(old_lines, new_lines, lineterm="", n=3):
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("@@"):
            lines.append({"type": "hunk", "text": ln})
        elif ln.startswith("+"):
            lines.append({"type": "add", "text": ln[1:]})
            added += 1
        elif ln.startswith("-"):
            lines.append({"type": "del", "text": ln[1:]})
            removed += 1
        else:
            lines.append({"type": "ctx", "text": ln[1:] if ln else ln})

    MAX_LINES = 5000
    truncated = len(lines) > MAX_LINES
    result = {
        "old": old,
        "new": new,
        "added": added,
        "removed": removed,
        "identical": added == 0 and removed == 0,
        "has_text": bool(old_lines or new_lines),
        "truncated": truncated,
        "lines": lines[:MAX_LINES],
    }
    _admin_cache.set(cache_key, result)
    return result


@app.get("/api/admin/missing-documents")
async def get_missing_documents(
    request: Request,
    x_api_key: str = Header(None),
    dataset: int = None
):
    """Get all missing (404) documents (requires admin authentication)
    
    Query params:
        dataset: Filter by dataset number (optional)
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_missing_documents(dataset_num=dataset)

    missing_docs = await asyncio.to_thread(_work)

    return {
        "missing_documents": missing_docs,
        "total": len(missing_docs)
    }


@app.get("/api/admin/doj-manifest")
async def get_doj_manifest(
    request: Request,
    x_api_key: str = Header(None),
    dataset: int = None,
    status: str = None
):
    """Get DOJ manifest entries (requires admin authentication)
    
    Query params:
        dataset: Filter by dataset number (optional)
        status: Filter by status - 'found', 'downloaded', '404', 'failed' (optional)
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_manifest(dataset_num=dataset, status=status)

    manifest = await asyncio.to_thread(_work)

    return {
        "manifest": manifest,
        "total": len(manifest)
    }


@app.get("/api/admin/not-downloaded")
async def get_not_downloaded(
    request: Request,
    x_api_key: str = Header(None),
    dataset: int = None
):
    """Get files that are not successfully downloaded (requires admin authentication)
    
    Query params:
        dataset: Filter by dataset number (optional)
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    def _work():
        return db.get_not_downloaded(dataset_num=dataset)

    not_downloaded = await asyncio.to_thread(_work)

    return {
        "not_downloaded": not_downloaded,
        "total": len(not_downloaded)
    }


@app.delete("/api/admin/missing-documents/{filename}")
async def remove_missing_document(
    filename: str,
    request: Request,
    x_api_key: str = Header(None),
    dataset: int = None
):
    """Remove a document from missing list (requires admin authentication)
    
    Use this if a file becomes available later.
    """
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    if dataset is None:
        raise HTTPException(status_code=400, detail="dataset query parameter required")
    
    client_ip, request_id = get_client_info(request)

    def _work():
        return db.remove_missing_document(filename, dataset)

    success = await asyncio.to_thread(_work)

    if success:
        security_logger.log_admin_action(
            client_ip=client_ip,
            request_id=request_id,
            action="remove_missing_document",
            target=filename,
            details=f"Removed from dataset {dataset}"
        )
    
    return {"success": success}


# Mount static files last (if frontend exists)
if STATIC_PATH.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_PATH)), name="static")


if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    
    print(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


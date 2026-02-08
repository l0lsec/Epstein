"""
Epstein Files Search Platform - API Server
FastAPI backend for document search and LLM-powered analysis
"""

import os
import json
import asyncio
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


# Allowed referers for document downloads (anti-scraping)
# Includes: own domain, localhost, social media in-app browsers, ChatGPT
ALLOWED_REFERERS = os.getenv(
    "ALLOWED_REFERERS", 
    ",".join([
        # Own domain
        "https://epsteinfta.com",
        "https://www.epsteinfta.com",
        "http://localhost",
        # Social media (in-app browsers)
        "https://www.facebook.com",
        "https://m.facebook.com",
        "https://l.facebook.com",
        "https://lm.facebook.com",
        "https://twitter.com",
        "https://x.com",
        "https://t.co",
        "https://www.instagram.com",
        "https://l.instagram.com",
        "https://www.linkedin.com",
        "https://www.reddit.com",
        "https://old.reddit.com",
        "https://www.tiktok.com",
        # ChatGPT / OpenAI
        "https://chat.openai.com",
        "https://chatgpt.com",
    ])
).split(",")


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
    
    def set(self, key: str, data):
        self._cache[key] = (data, datetime.now().timestamp())
    
    def invalidate(self, key: str = None):
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()

# Cache instances with different TTLs
_stats_cache = ResponseCache(ttl_seconds=30)  # Stats cached for 30 seconds
_categories_cache = ResponseCache(ttl_seconds=60)  # Categories cached for 60 seconds
_bootstrap_cache = ResponseCache(ttl_seconds=30)  # Bootstrap (stats+categories+keywords+settings) for 30s
_maintenance_cache = ResponseCache(ttl_seconds=5)  # Maintenance/status-page check (avoids DB hit per request)

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
        doc_count = db.get_stats()['total_documents']
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
    # Second check: custom status page enabled in database settings (cached to avoid DB hit per request)
    elif db:
        status_enabled = _maintenance_cache.get("status_page_enabled")
        if status_enabled is None:
            status_enabled = db.get_setting("status_page_enabled", "false")
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
    file_type: Optional[str] = None  # "pdf", "audio", "video"
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
async def root(doc: str = None):
    """Serve the frontend with dynamic OG tags for document shares"""
    index_path = STATIC_PATH / "index.html"
    if not index_path.exists():
        return {"message": "Epstein Files Search Platform API", "docs": "/docs"}
    
    # If doc param present, serve modified HTML with document's thumbnail for social sharing
    if doc and db:
        document = db.get_document(doc)
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
                    'content="Epstein Files Library Archive | Public Document Search"',
                    f'content="{filename_escaped} | Epstein Files Archive"'
                )
                # Replace twitter:title as well
                html = html.replace(
                    'content="Epstein Files Library Archive | Public Document Search">',
                    f'content="{filename_escaped} | Epstein Files Archive">'
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
    return get_maintenance_status_data()


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
            
            # Get current status
            current_status = get_maintenance_status_data()
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
    """Get platform statistics (cached for 30 seconds)"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Check cache first
    cached = _stats_cache.get("stats")
    if cached:
        response.headers["X-Cache"] = "HIT"
        response.headers["Cache-Control"] = "public, max-age=30, s-maxage=30"
        return cached
    
    stats = db.get_stats()
    result = StatsResponse(
        total_documents=stats["total_documents"],
        total_pages=stats["total_pages"],
        by_category=stats["by_category"],
        by_subcategory=stats["by_subcategory"],
        by_file_type=stats.get("by_file_type", []),
        vector_chunks=vector_store.get_count() if vector_store else 0,
        llm_available=llm.is_available() if llm else False
    )
    
    # Cache the result
    _stats_cache.set("stats", result)
    response.headers["X-Cache"] = "MISS"
    response.headers["Cache-Control"] = "public, max-age=30, s-maxage=30"
    return result


@app.get("/api/bootstrap")
async def get_bootstrap(response: Response):
    """Single request for initial page load: stats, categories, keywords, and public settings.
    Reduces 4 round-trips to 1 for faster first paint."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    cached = _bootstrap_cache.get("bootstrap")
    if cached:
        response.headers["X-Cache"] = "HIT"
        response.headers["Cache-Control"] = "public, max-age=30, s-maxage=30"
        return cached
    
    # Build bootstrap in one go
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
    }
    
    # Prefetch first browse page so the Browse tab is instant
    browse_limit = 24
    browse_docs, browse_total = db.get_all_documents_with_total(
        limit=browse_limit, offset=0, include_hidden=False
    )
    # Cache the unfiltered total for subsequent page requests
    _categories_cache.set("docs_total_unfiltered", browse_total)
    
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
    }
    _bootstrap_cache.set("bootstrap", result)
    response.headers["X-Cache"] = "MISS"
    response.headers["Cache-Control"] = "public, max-age=30, s-maxage=30"
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
async def get_categories(keyword: Optional[str] = None, response: Response = None):
    """Get all document categories, optionally filtered by keyword (cached for 60 seconds)
    
    Note: Hidden categories and documents in hidden categories are excluded from results.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Use cache only for unfiltered requests (most common)
    cache_key = f"categories:{keyword or 'all'}"
    if not keyword:
        cached = _categories_cache.get(cache_key)
        if cached:
            if response:
                response.headers["X-Cache"] = "HIT"
                response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
            return cached
    
    # Exclude hidden categories and hidden documents from public view
    categories = db.get_category_counts(keyword=keyword, include_hidden=False)
    result = {"categories": categories}
    
    # Cache unfiltered results
    if not keyword:
        _categories_cache.set(cache_key, result)
    
    if response:
        response.headers["X-Cache"] = "MISS" if not keyword else "BYPASS"
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    return result


@app.get("/api/subcategories")
async def get_subcategories(category: Optional[str] = None):
    """Get subcategories, optionally filtered by category
    
    Note: Returns empty list if the category is hidden.
    Uses a dedicated lightweight query instead of full stats.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # If category is specified and it's hidden, return empty
    if category and db.is_category_hidden(category):
        return {"subcategories": []}
    
    subcategories = db.get_subcategory_counts(category=category, include_hidden=False)
    # Normalize to list of {subcategory, count} for the requested category only
    if category:
        subcategories = [{"subcategory": s["subcategory"], "count": s["count"]} for s in subcategories if s.get("subcategory")]
    else:
        subcategories = [{"subcategory": s["subcategory"], "count": s["count"]} for s in subcategories if s.get("subcategory")]
    
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
    
    # Parse the query for Boolean operators (for full-text search)
    parsed = parse_boolean_query(search_request.query)
    fts_query = parsed['fts_query']
    
    if search_request.search_type in ["fulltext", "hybrid"]:
        # Full-text search using parsed Boolean query
        # Note: Hidden documents and hidden categories are excluded from results
        try:
            ft_results = db.search_fulltext(
                query=fts_query,
                limit=search_request.limit,
                offset=search_request.offset,
                category=search_request.category,
                subcategory=search_request.subcategory,
                file_type=search_request.file_type,
                date_from=search_request.date_from,
                date_to=search_request.date_to,
                include_hidden=False
            )
            # Get actual total count for pagination
            total_count = db.count_fulltext_results(
                query=fts_query,
                category=search_request.category,
                subcategory=search_request.subcategory,
                file_type=search_request.file_type,
                date_from=search_request.date_from,
                date_to=search_request.date_to,
                include_hidden=False
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
        # Note: Validate docs exist in DB and are visible to avoid stale/hidden entries
        existing_ids = {r["id"] for r in results}
        for r in sem_results:
            doc_id = r.get("id", "")
            if doc_id and doc_id not in existing_ids:
                # Verify document exists and is visible (not hidden)
                if not db.is_document_visible(doc_id):
                    continue
                # Use metadata-only to avoid loading full_text for every result
                doc = db.get_document(doc_id, include_full_text=False)
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
    
    # Get faceted counts for filter dropdowns (excluding hidden content)
    facets = {}
    if search_request.search_type in ["fulltext", "hybrid"]:
        try:
            facets = db.get_search_facets(
                query=fts_query,
                category=search_request.category,
                subcategory=search_request.subcategory,
                file_type=search_request.file_type,
                date_from=search_request.date_from,
                date_to=search_request.date_to,
                include_hidden=False
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
    
    if unfiltered:
        # Single query for list + total (get_all_documents_with_total); cache total for later pages
        cached_total = _categories_cache.get("docs_total_unfiltered")
        if cached_total is not None:
            docs = db.get_all_documents(limit=limit, offset=offset, include_hidden=False)
            total = cached_total
        else:
            docs, total = db.get_all_documents_with_total(
                limit=limit, offset=offset, include_hidden=False
            )
            _categories_cache.set("docs_total_unfiltered", total)
    elif not keyword and not search:
        # Filtered but no FTS: still use single query for list + total
        docs, total = db.get_all_documents_with_total(
            limit=limit, offset=offset,
            category=category, subcategory=subcategory, file_type=file_type, filename=filename,
            include_hidden=False,
        )
    else:
        # Keyword or search: keep two-call path (FTS join)
        docs = db.get_all_documents(limit=limit, offset=offset, category=category, subcategory=subcategory, file_type=file_type, filename=filename, keyword=keyword, search=search, include_hidden=False)
        total = db.count_documents(category=category, subcategory=subcategory, file_type=file_type, filename=filename, keyword=keyword, include_hidden=False)
    
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
    search_type: str = "fulltext"
):
    """Export documents as a list for CSV download
    
    Returns all matching documents (up to 50,000) with filename, category, 
    subcategory, and DOJ direct link (if available in manifest).
    
    Args:
        category: Filter by category
        subcategory: Filter by subcategory
        file_type: Filter by file type
        filename: Partial filename match
        keyword: Keyword search
        search_query: Full-text search query
        search_type: Type of search (fulltext, semantic, hybrid)
    
    Returns:
        JSON with documents array containing filename, category, subcategory, doj_url
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Parse search query for FTS if provided
    fts_query = None
    if search_query and search_type in ["fulltext", "hybrid"]:
        parsed = parse_boolean_query(search_query)
        fts_query = parsed['fts_query']
    
    try:
        documents = db.get_documents_for_export(
            category=category,
            subcategory=subcategory,
            file_type=file_type,
            filename=filename,
            keyword=keyword,
            search_query=fts_query
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
    doc = db.get_document(doc_id, include_hidden=False, include_full_text=include_text)
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


@app.get("/api/documents/{doc_id}/text")
async def get_document_text(doc_id: str, request: Request):
    """Get only the full text of a document (for lazy loading in modal).
    
    Note: Returns 404 for hidden documents or documents in hidden categories.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    full_text = db.get_document_full_text(doc_id, include_hidden=False)
    if full_text is None:
        raise HTTPException(status_code=404, detail="Document not found")
    
    return {"full_text": full_text}


@app.get("/api/documents/{doc_id}/file")
async def get_document_file(doc_id: str, request: Request):
    """Get the actual document file for inline viewing
    
    Note: Returns 404 for hidden documents or documents in hidden categories.
    """
    client_ip, request_id = get_client_info(request)
    
    # Referer validation - redirect direct API access to main site
    referer = request.headers.get("referer", "")
    if not any(referer.startswith(allowed) for allowed in ALLOWED_REFERERS):
        # Log with full referer for triage - helps identify legitimate referers to add
        security_logger.log_security_event(
            event_type="referer_redirect",
            severity="medium",
            client_ip=client_ip,
            message=f"Document access redirected - referer not in allowlist",
            request_id=request_id,
            document_id=doc_id,
            referer=referer[:200] if referer else "EMPTY",
            user_agent=request.headers.get("user-agent", "")[:200]
        )
        # Redirect to main site with document context
        return RedirectResponse(
            url=f"https://epsteinfta.com/?doc={doc_id}",
            status_code=302
        )
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # CRITICAL: Check if document is visible (not hidden and category not hidden)
    doc = db.get_document(doc_id, include_hidden=False)
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
    elif file_type == "image":
        if not generate_image_thumbnail(file_path, thumbnail_path):
            create_placeholder_thumbnail("image", thumbnail_path)
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
            headers={
                'Cache-Control': 'public, max-age=86400',  # Cache for 24 hours
            }
        )
    
    # Cache miss – need to generate the thumbnail. Verify visibility first.
    doc = db.get_document(doc_id, include_hidden=False, include_full_text=False)
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
            headers={
                'Cache-Control': 'public, max-age=86400',  # Cache for 24 hours
            }
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
    
    # Get relevant documents via semantic search
    context_docs = vector_store.search(
        query=ask_request.question,
        n_results=ask_request.num_context_docs,
        category=ask_request.category
    )
    
    # Filter out hidden documents from AI context (SECURITY: prevent hidden doc content from being exposed via AI)
    visible_context_docs = [doc for doc in context_docs if db.is_document_visible(doc.get("id", ""))]
    
    # Log the LLM query
    security_logger.log_llm_query(
        client_ip=client_ip,
        question=ask_request.question,
        context_docs_count=len(visible_context_docs),
        request_id=request_id,
        category=ask_request.category
    )
    
    if not visible_context_docs:
        return {
            "question": ask_request.question,
            "answer": "No relevant documents found for this question.",
            "sources": []
        }
    
    # Fetch full document content from database for better accuracy
    # The vector store only has 500-char snippets, we need full text for LLM
    enriched_docs = []
    for doc in visible_context_docs:
        doc_id = doc.get("id")
        if doc_id and db:
            full_doc = db.get_document(doc_id, include_hidden=False)
            if full_doc:
                enriched_docs.append({
                    **doc,
                    "full_text": full_doc.get("full_text", doc.get("text", "")),
                    "filename": full_doc.get("filename", doc.get("filename", "Unknown")),
                    "category": full_doc.get("category", doc.get("category", "Unknown")),
                    "subcategory": full_doc.get("subcategory", doc.get("subcategory", ""))
                })
            # Skip if document no longer visible (was hidden between search and fetch)
        else:
            # Vector store doc without DB lookup - skip for safety
            pass
    
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
    
    # Get relevant documents via semantic search
    context_docs = vector_store.search(
        query=ask_request.question,
        n_results=ask_request.num_context_docs,
        category=ask_request.category
    )
    
    # Filter out hidden documents from AI context (SECURITY: prevent hidden doc content from being exposed via AI)
    visible_context_docs = [doc for doc in context_docs if db.is_document_visible(doc.get("id", ""))]
    
    # Fetch full document content from database for better accuracy
    enriched_docs = []
    for doc in visible_context_docs:
        doc_id = doc.get("id")
        if doc_id and db:
            full_doc = db.get_document(doc_id, include_hidden=False)
            if full_doc:
                enriched_docs.append({
                    **doc,
                    "full_text": full_doc.get("full_text", doc.get("text", "")),
                    "filename": full_doc.get("filename", doc.get("filename", "Unknown")),
                    "category": full_doc.get("category", doc.get("category", "Unknown")),
                    "subcategory": full_doc.get("subcategory", doc.get("subcategory", ""))
                })
            # Skip if document no longer visible
    
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
    
    # Check if document is visible (not hidden and category not hidden)
    doc = db.get_document(doc_id, include_hidden=False)
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


# =============================================================================
# ADMIN TELEMETRY ENDPOINTS
# =============================================================================

LOG_DIR = BASE_PATH / "logs"


def parse_log_file(log_path: Path, max_lines: int = 10000) -> List[dict]:
    """Parse a JSON log file and return list of log entries"""
    if not log_path.exists():
        return []
    
    entries = []
    try:
        with open(log_path, 'r') as f:
            lines = f.readlines()
            # Get the last max_lines entries
            for line in lines[-max_lines:]:
                try:
                    entry = json.loads(line.strip())
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return entries


def get_time_buckets(entries: List[dict], bucket_minutes: int = 5, max_buckets: int = 288) -> dict:
    """Group log entries into time buckets for charting"""
    from collections import defaultdict
    
    buckets = defaultdict(lambda: {"count": 0, "errors": 0, "avg_duration": 0, "durations": []})
    
    for entry in entries:
        try:
            timestamp = entry.get("timestamp", "")
            if not timestamp:
                continue
            
            # Parse ISO timestamp
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            # Round to bucket
            bucket_key = dt.replace(
                minute=(dt.minute // bucket_minutes) * bucket_minutes,
                second=0,
                microsecond=0
            ).isoformat()
            
            buckets[bucket_key]["count"] += 1
            
            if entry.get("status_code", 200) >= 400:
                buckets[bucket_key]["errors"] += 1
            
            duration = entry.get("duration_ms")
            if duration:
                buckets[bucket_key]["durations"].append(duration)
        except Exception:
            continue
    
    # Calculate averages and sort
    result = []
    for key in sorted(buckets.keys())[-max_buckets:]:
        bucket = buckets[key]
        avg_duration = sum(bucket["durations"]) / len(bucket["durations"]) if bucket["durations"] else 0
        result.append({
            "time": key,
            "requests": bucket["count"],
            "errors": bucket["errors"],
            "avg_duration_ms": round(avg_duration, 2)
        })
    
    return result


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
    # Verify admin access
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    # Parse access logs
    access_entries = parse_log_file(LOG_DIR / "access.log", max_lines=50000)
    security_entries = parse_log_file(LOG_DIR / "security.log", max_lines=10000)
    audit_entries = parse_log_file(LOG_DIR / "audit.log", max_lines=10000)
    error_entries = parse_log_file(LOG_DIR / "error.log", max_lines=1000)
    
    # Calculate time periods
    now = datetime.utcnow()
    hour_ago = (now - timedelta(hours=1)).isoformat()
    day_ago = (now - timedelta(days=1)).isoformat()
    
    # Filter entries by time
    last_hour = [e for e in access_entries if e.get("timestamp", "") >= hour_ago]
    last_day = [e for e in access_entries if e.get("timestamp", "") >= day_ago]
    
    # Calculate metrics
    total_requests = len(access_entries)
    requests_last_hour = len(last_hour)
    requests_last_day = len(last_day)
    
    # Status code breakdown
    status_codes = {}
    for entry in access_entries:
        code = str(entry.get("status_code", "unknown"))
        status_codes[code] = status_codes.get(code, 0) + 1
    
    # Error rate
    errors = sum(1 for e in access_entries if e.get("status_code", 200) >= 400)
    error_rate = (errors / total_requests * 100) if total_requests > 0 else 0
    
    # Average response time
    durations = [e.get("duration_ms", 0) for e in access_entries if e.get("duration_ms")]
    avg_duration = sum(durations) / len(durations) if durations else 0
    
    # Unique IPs
    unique_ips = len(set(e.get("client_ip", "") for e in access_entries))
    unique_ips_hour = len(set(e.get("client_ip", "") for e in last_hour))
    
    # Top endpoints
    endpoint_counts = {}
    for entry in access_entries:
        path = entry.get("path", "")
        endpoint_counts[path] = endpoint_counts.get(path, 0) + 1
    top_endpoints = sorted(endpoint_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Security events count
    security_events = len(security_entries)
    security_high = len([e for e in security_entries if e.get("severity") in ["high", "critical"]])
    
    # Rate limited requests
    rate_limited = len([e for e in access_entries if e.get("rate_limited")])
    
    # Get session stats from security logger
    from security_logger import get_session_stats, get_blocked_ips, get_blocked_sessions
    session_stats = get_session_stats()
    blocked_ips_count = len(get_blocked_ips())
    blocked_sessions_count = len(get_blocked_sessions())
    
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
            "security_events": security_events,
            "security_events_high": security_high,
            "rate_limited_requests": rate_limited,
            "error_count": len(error_entries)
        },
        "status_codes": status_codes,
        "top_endpoints": [{"path": p, "count": c} for p, c in top_endpoints],
        "sessions": {
            "active_sessions": session_stats.get("active_sessions", 0),
            "blocked_sessions": blocked_sessions_count,
            "blocked_ips": blocked_ips_count
        }
    }


@app.get("/api/admin/telemetry/requests")
async def get_request_telemetry(
    request: Request,
    timeframe: str = "1h",  # 1h, 6h, 24h, 7d
    x_api_key: str = Header(None)
):
    """Get request telemetry with time series data (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    # Parse access logs
    access_entries = parse_log_file(LOG_DIR / "access.log", max_lines=100000)
    
    now = datetime.utcnow()
    
    # Determine time filter
    if timeframe == "1h":
        cutoff = (now - timedelta(hours=1)).isoformat()
        bucket_minutes = 1
    elif timeframe == "6h":
        cutoff = (now - timedelta(hours=6)).isoformat()
        bucket_minutes = 5
    elif timeframe == "24h":
        cutoff = (now - timedelta(hours=24)).isoformat()
        bucket_minutes = 15
    elif timeframe == "7d":
        cutoff = (now - timedelta(days=7)).isoformat()
        bucket_minutes = 60
    else:
        cutoff = (now - timedelta(hours=1)).isoformat()
        bucket_minutes = 1
    
    filtered = [e for e in access_entries if e.get("timestamp", "") >= cutoff]
    
    # Get time series
    time_series = get_time_buckets(filtered, bucket_minutes=bucket_minutes)
    
    # Method breakdown
    methods = {}
    for entry in filtered:
        method = entry.get("method", "UNKNOWN")
        methods[method] = methods.get(method, 0) + 1
    
    # Response time distribution
    durations = [e.get("duration_ms", 0) for e in filtered if e.get("duration_ms")]
    duration_buckets = {
        "0-50ms": len([d for d in durations if d < 50]),
        "50-100ms": len([d for d in durations if 50 <= d < 100]),
        "100-500ms": len([d for d in durations if 100 <= d < 500]),
        "500ms-1s": len([d for d in durations if 500 <= d < 1000]),
        "1s-5s": len([d for d in durations if 1000 <= d < 5000]),
        "5s+": len([d for d in durations if d >= 5000])
    }
    
    # Get recent requests with details (last 50)
    recent_requests = []
    for entry in filtered[-50:]:
        recent_requests.append({
            "timestamp": entry.get("timestamp"),
            "client_ip": entry.get("client_ip", "unknown"),
            "path": entry.get("path", ""),
            "method": entry.get("method", ""),
            "status_code": entry.get("status_code"),
            "user_agent": entry.get("user_agent", "")[:150],
            "duration_ms": entry.get("duration_ms")
        })
    recent_requests.reverse()  # Most recent first
    
    # Enrich with geolocation data
    await enrich_with_geo(recent_requests, 'client_ip', limit=30)
    
    return {
        "timeframe": timeframe,
        "total_requests": len(filtered),
        "time_series": time_series,
        "methods": methods,
        "response_time_distribution": duration_buckets,
        "recent_requests": recent_requests
    }


@app.get("/api/admin/telemetry/search")
async def get_search_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get search-specific telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    audit_entries = parse_log_file(LOG_DIR / "audit.log", max_lines=50000)
    
    # Filter search queries
    search_entries = [e for e in audit_entries if e.get("event_type") == "search_query"]
    
    # Search type breakdown
    search_types = {}
    for entry in search_entries:
        st = entry.get("search_type", "unknown")
        search_types[st] = search_types.get(st, 0) + 1
    
    # Top search queries
    query_counts = {}
    for entry in search_entries:
        query = entry.get("query", "")[:100]  # Truncate
        if query:
            query_counts[query] = query_counts.get(query, 0) + 1
    top_queries = sorted(query_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    
    # Category filter usage
    category_usage = {}
    for entry in search_entries:
        cat = entry.get("category")
        if cat:
            category_usage[cat] = category_usage.get(cat, 0) + 1
    
    # File type filter usage
    file_type_usage = {}
    for entry in search_entries:
        ft = entry.get("file_type")
        if ft:
            file_type_usage[ft] = file_type_usage.get(ft, 0) + 1
    
    # Results statistics
    result_counts = [e.get("result_count", 0) for e in search_entries]
    avg_results = sum(result_counts) / len(result_counts) if result_counts else 0
    zero_result_searches = len([r for r in result_counts if r == 0])
    
    return {
        "total_searches": len(search_entries),
        "search_types": search_types,
        "top_queries": [{"query": q, "count": c} for q, c in top_queries],
        "category_usage": category_usage,
        "file_type_usage": file_type_usage,
        "avg_results_per_search": round(avg_results, 2),
        "zero_result_searches": zero_result_searches
    }


@app.get("/api/admin/telemetry/documents")
async def get_document_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get document access telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    audit_entries = parse_log_file(LOG_DIR / "audit.log", max_lines=50000)
    
    # Filter document access
    doc_entries = [e for e in audit_entries if e.get("event_type") == "document_access"]
    
    # Access type breakdown
    access_types = {}
    for entry in doc_entries:
        action = entry.get("action", "unknown")
        access_types[action] = access_types.get(action, 0) + 1
    
    # Top accessed documents
    doc_counts = {}
    for entry in doc_entries:
        filename = entry.get("filename", "")
        if filename:
            doc_counts[filename] = doc_counts.get(filename, 0) + 1
    top_docs = sorted(doc_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    
    # File type distribution
    file_types = {}
    for entry in doc_entries:
        ft = entry.get("file_type", ".pdf")
        file_types[ft] = file_types.get(ft, 0) + 1
    
    # Total data served (approximate)
    total_bytes = sum(e.get("file_size_bytes", 0) for e in doc_entries)
    
    return {
        "total_document_accesses": len(doc_entries),
        "access_types": access_types,
        "top_documents": [{"filename": f, "count": c} for f, c in top_docs],
        "file_types": file_types,
        "total_data_served_mb": round(total_bytes / (1024 * 1024), 2)
    }


@app.get("/api/admin/telemetry/ai")
async def get_ai_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get AI/LLM usage telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    audit_entries = parse_log_file(LOG_DIR / "audit.log", max_lines=50000)
    
    # Filter LLM queries
    llm_entries = [e for e in audit_entries if e.get("event_type") == "llm_query"]
    
    # Summary generation
    summary_entries = [e for e in audit_entries 
                       if e.get("event_type") == "document_access" 
                       and e.get("action") in ["summary_generate", "summary_cached"]]
    
    # Top questions
    question_counts = {}
    for entry in llm_entries:
        question = entry.get("question", "")[:100]
        if question:
            question_counts[question] = question_counts.get(question, 0) + 1
    top_questions = sorted(question_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    
    # Streaming vs non-streaming
    streaming_count = len([e for e in llm_entries if e.get("streaming")])
    
    return {
        "total_ai_queries": len(llm_entries),
        "total_summaries": len(summary_entries),
        "streaming_queries": streaming_count,
        "non_streaming_queries": len(llm_entries) - streaming_count,
        "top_questions": [{"question": q, "count": c} for q, c in top_questions]
    }


@app.get("/api/admin/telemetry/security")
async def get_security_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get security events telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    security_entries = parse_log_file(LOG_DIR / "security.log", max_lines=10000)
    
    # Event type breakdown
    event_types = {}
    for entry in security_entries:
        et = entry.get("event_type", "unknown")
        event_types[et] = event_types.get(et, 0) + 1
    
    # Severity breakdown
    severities = {}
    for entry in security_entries:
        sev = entry.get("severity", "unknown")
        severities[sev] = severities.get(sev, 0) + 1
    
    # Recent high-severity events
    high_severity = [
        {
            "timestamp": e.get("timestamp"),
            "event_type": e.get("event_type"),
            "message": e.get("message", "")[:200],
            "client_ip": e.get("client_ip")
        }
        for e in security_entries 
        if e.get("severity") in ["high", "critical"]
    ][-20:]
    
    # Rate limit violations
    rate_limit_events = [e for e in security_entries if e.get("event_type") == "rate_limit_exceeded"]
    
    # Suspicious activity
    suspicious = [e for e in security_entries if e.get("event_type") == "suspicious_activity"]
    
    # IPs with most security events
    ip_events = {}
    for entry in security_entries:
        ip = entry.get("client_ip")
        if ip:
            ip_events[ip] = ip_events.get(ip, 0) + 1
    top_ips = sorted(ip_events.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Get blocked IPs and sessions
    from security_logger import get_blocked_ips, get_blocked_sessions
    
    return {
        "total_security_events": len(security_entries),
        "event_types": event_types,
        "severities": severities,
        "recent_high_severity": high_severity,
        "rate_limit_violations": len(rate_limit_events),
        "suspicious_activities": len(suspicious),
        "top_ips_by_events": [{"ip": ip, "count": c} for ip, c in top_ips],
        "blocked_ips": list(get_blocked_ips()),
        "blocked_sessions": len(get_blocked_sessions())
    }


@app.get("/api/admin/telemetry/errors")
async def get_error_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get error logs telemetry (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    error_entries = parse_log_file(LOG_DIR / "error.log", max_lines=1000)
    
    # Error type breakdown
    error_types = {}
    for entry in error_entries:
        et = entry.get("error_type", "unknown")
        error_types[et] = error_types.get(et, 0) + 1
    
    # Context breakdown (where errors occur)
    contexts = {}
    for entry in error_entries:
        ctx = entry.get("context", "unknown")
        contexts[ctx] = contexts.get(ctx, 0) + 1
    
    # Recent errors
    recent_errors = [
        {
            "timestamp": e.get("timestamp"),
            "error_type": e.get("error_type"),
            "context": e.get("context"),
            "message": e.get("error_message", "")[:200]
        }
        for e in error_entries
    ][-20:]
    
    return {
        "total_errors": len(error_entries),
        "error_types": error_types,
        "error_contexts": contexts,
        "recent_errors": recent_errors
    }


@app.get("/api/admin/telemetry/visitors")
async def get_visitor_telemetry(request: Request, x_api_key: str = Header(None)):
    """Get visitor/user analytics (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    access_entries = parse_log_file(LOG_DIR / "access.log", max_lines=100000)
    
    now = datetime.utcnow()
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    
    # Filter by time
    last_day = [e for e in access_entries if e.get("timestamp", "") >= day_ago]
    last_week = [e for e in access_entries if e.get("timestamp", "") >= week_ago]
    
    # Unique IPs per day (last 7 days)
    from collections import defaultdict
    daily_visitors = defaultdict(set)
    for entry in last_week:
        try:
            ts = entry.get("timestamp", "")
            if ts:
                date = ts[:10]  # YYYY-MM-DD
                ip = entry.get("client_ip", "")
                if ip:
                    daily_visitors[date].add(ip)
        except Exception:
            continue
    
    daily_unique = sorted([
        {"date": date, "unique_visitors": len(ips)}
        for date, ips in daily_visitors.items()
    ], key=lambda x: x["date"])
    
    # User agents
    user_agents = {}
    for entry in last_day:
        ua = entry.get("user_agent", "")[:100]
        if ua:
            # Simplify user agent
            if "Chrome" in ua:
                browser = "Chrome"
            elif "Firefox" in ua:
                browser = "Firefox"
            elif "Safari" in ua:
                browser = "Safari"
            elif "curl" in ua:
                browser = "curl/CLI"
            elif "bot" in ua.lower() or "spider" in ua.lower():
                browser = "Bot/Crawler"
            else:
                browser = "Other"
            user_agents[browser] = user_agents.get(browser, 0) + 1
    
    # Referrers
    referrers = {}
    for entry in last_day:
        ref = entry.get("referer", "")
        if ref and "localhost" not in ref and "127.0.0.1" not in ref:
            # Extract domain
            try:
                from urllib.parse import urlparse
                domain = urlparse(ref).netloc or "Direct"
            except Exception:
                domain = "Direct"
            referrers[domain] = referrers.get(domain, 0) + 1
    
    top_referrers = sorted(referrers.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Top IPs by request count
    ip_counts = {}
    for entry in last_day:
        ip = entry.get("client_ip", "")
        if ip and ip != "unknown":
            ip_counts[ip] = ip_counts.get(ip, 0) + 1
    
    top_ips_list = sorted(ip_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    top_ips = [{"ip": ip, "client_ip": ip, "count": c} for ip, c in top_ips_list]
    
    # Enrich top IPs with geolocation data
    await enrich_with_geo(top_ips, 'client_ip', limit=20)
    
    return {
        "unique_visitors_today": len(set(e.get("client_ip", "") for e in last_day)),
        "unique_visitors_week": len(set(e.get("client_ip", "") for e in last_week)),
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
    
    # Database stats
    db_stats = db.get_stats() if db else {}
    
    # Log file sizes
    log_sizes = {}
    for log_file in ["access.log", "security.log", "audit.log", "error.log"]:
        log_path = LOG_DIR / log_file
        if log_path.exists():
            log_sizes[log_file] = round(log_path.stat().st_size / (1024 * 1024), 2)  # MB
    
    # Uptime (approximated from first log entry)
    access_entries = parse_log_file(LOG_DIR / "access.log", max_lines=1)
    first_request = access_entries[0].get("timestamp") if access_entries else None
    
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
    
    audit_entries = parse_log_file(LOG_DIR / "audit.log", max_lines=50000)
    
    # Filter for summary generation events
    summary_entries = [
        e for e in audit_entries 
        if e.get("event_type") == "document_access" 
        and e.get("action") in ["summary_generate", "summary_cached"]
    ]
    
    # Group by document
    doc_summaries = {}
    for entry in summary_entries:
        doc_id = entry.get("document_id", "")
        filename = entry.get("filename", "Unknown")
        
        if doc_id not in doc_summaries:
            doc_summaries[doc_id] = {
                "document_id": doc_id,
                "filename": filename,
                "generated_count": 0,
                "cached_count": 0,
                "last_generated": None,
                "first_generated": None
            }
        
        if entry.get("action") == "summary_generate":
            doc_summaries[doc_id]["generated_count"] += 1
        else:
            doc_summaries[doc_id]["cached_count"] += 1
        
        timestamp = entry.get("timestamp", "")
        if timestamp:
            if not doc_summaries[doc_id]["first_generated"] or timestamp < doc_summaries[doc_id]["first_generated"]:
                doc_summaries[doc_id]["first_generated"] = timestamp
            if not doc_summaries[doc_id]["last_generated"] or timestamp > doc_summaries[doc_id]["last_generated"]:
                doc_summaries[doc_id]["last_generated"] = timestamp
    
    # Sort by most recently generated
    sorted_docs = sorted(
        doc_summaries.values(),
        key=lambda x: x["last_generated"] or "",
        reverse=True
    )
    
    return {
        "total_documents_with_summaries": len(sorted_docs),
        "total_generations": sum(d["generated_count"] for d in sorted_docs),
        "total_cache_hits": sum(d["cached_count"] for d in sorted_docs),
        "documents": sorted_docs[:50]  # Return top 50 most recent
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
            "ip": fb.get("ip", "Unknown")
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


# =============================================================================
# SETTINGS API ENDPOINTS
# =============================================================================

@app.get("/api/settings")
async def get_public_settings():
    """Get public settings (no auth required)"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    settings = db.get_all_settings()
    
    # Only expose certain settings to the public
    public_settings = {
        "ask_ai_enabled": settings.get("ask_ai_enabled", "true") == "true",
        "pinned_documents_enabled": settings.get("pinned_documents_enabled", "true") == "true"
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
    
    return db.get_all_settings()


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
    
    db.set_setting(setting.key, setting.value)
    
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
    
    return {
        "enabled": db.get_setting("status_page_enabled", "false") == "true",
        "title": db.get_setting("status_page_title", "Under Maintenance"),
        "message": db.get_setting("status_page_message", "We're performing scheduled maintenance. Please check back soon."),
        "timeline": db.get_setting("status_page_timeline", ""),
        "started": db.get_setting("status_page_started", ""),
        "indexing_active": MAINTENANCE_LOCK.exists()  # Whether .maintenance file exists
    }


@app.post("/api/admin/status-page")
async def update_status_page(status: StatusPageUpdate, request: Request, x_api_key: str = Header(None)):
    """Update status page settings (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    client_ip, request_id = get_client_info(request)
    
    # Update settings
    db.set_setting("status_page_enabled", "true" if status.enabled else "false")
    _maintenance_cache.invalidate("status_page_enabled")  # Clear cached value
    db.set_setting("status_page_title", status.title or "Under Maintenance")
    db.set_setting("status_page_message", status.message or "")
    db.set_setting("status_page_timeline", status.timeline or "")
    
    # Set started timestamp when enabling
    if status.enabled:
        current_started = db.get_setting("status_page_started", "")
        if not current_started:
            db.set_setting("status_page_started", datetime.now().isoformat())
    else:
        # Clear started time when disabling
        db.set_setting("status_page_started", "")
    
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
    
    db.set_setting("status_page_enabled", "false")
    _maintenance_cache.invalidate("status_page_enabled")  # Clear cached value
    db.set_setting("status_page_started", "")
    
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
    return {"pinned_documents": db.get_pinned_documents(include_hidden=False)}


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
    
    success = db.pin_document(
        pin_request.document_id,
        pin_request.reason,
        pin_request.display_order
    )
    
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
    
    success = db.update_pinned_document(
        document_id,
        update_request.reason,
        update_request.display_order
    )
    
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
    
    success = db.unpin_document(document_id)
    
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
    
    # Admin sees all pinned documents including hidden ones
    return {"pinned_documents": db.get_pinned_documents(include_hidden=True)}


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
    
    docs = db.get_hidden_documents(limit=limit, offset=offset)
    total = db.count_hidden_documents()
    
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
    
    # Get document info for logging
    doc = db.get_document(doc_id, include_hidden=True)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    success = db.hide_document(doc_id)
    if success:
        # Invalidate caches
        _categories_cache.clear()
        
        security_logger.log_system_event(
            "document_hidden",
            f"Document hidden by admin: {doc_id} ({doc.get('filename', 'Unknown')})",
            document_id=doc_id
        )
        return {"success": True, "message": f"Document '{doc.get('filename', doc_id)}' is now hidden"}
    
    raise HTTPException(status_code=500, detail="Failed to hide document")


@app.post("/api/admin/documents/{doc_id}/unhide")
async def unhide_document(doc_id: str, request: Request, x_api_key: str = Header(None)):
    """Unhide a document (make visible to public) (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Get document info for logging
    doc = db.get_document(doc_id, include_hidden=True)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    success = db.unhide_document(doc_id)
    if success:
        # Invalidate caches
        _categories_cache.clear()
        
        security_logger.log_system_event(
            "document_unhidden",
            f"Document unhidden by admin: {doc_id} ({doc.get('filename', 'Unknown')})",
            document_id=doc_id
        )
        return {"success": True, "message": f"Document '{doc.get('filename', doc_id)}' is now visible"}
    
    raise HTTPException(status_code=500, detail="Failed to unhide document")


@app.get("/api/admin/hidden-categories")
async def get_hidden_categories(request: Request, x_api_key: str = Header(None)):
    """Get all hidden categories (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    return {"hidden_categories": db.get_hidden_categories()}


@app.get("/api/admin/categories-visibility")
async def get_categories_visibility(request: Request, x_api_key: str = Header(None)):
    """Get all categories with their visibility status (requires admin authentication)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    return {"categories": db.get_all_categories_with_visibility()}


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
    
    # Verify category exists
    category_counts = db.get_category_counts(include_hidden=True)
    if not any(c["category"] == category for c in category_counts):
        raise HTTPException(status_code=404, detail=f"Category '{category}' not found")
    
    success = db.hide_category(category)
    if success:
        # Invalidate caches
        _categories_cache.clear()
        
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
    
    success = db.unhide_category(category)
    if success:
        # Invalidate caches
        _categories_cache.clear()
        
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
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """Search documents for visibility management (admin only, includes hidden docs)"""
    is_authorized, error = verify_admin_access(request, x_api_key)
    if not is_authorized:
        raise HTTPException(status_code=401, detail=error)
    
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    # Get documents including hidden ones
    docs = db.get_all_documents(
        limit=limit, 
        offset=offset, 
        category=category, 
        search=search,
        include_hidden=True
    )
    total = db.count_documents(category=category, include_hidden=True)
    
    return {
        "documents": docs,
        "total": total,
        "limit": limit,
        "offset": offset
    }


# =============================================================================
# Keywords Endpoints
# =============================================================================

@app.get("/api/keywords")
async def get_keywords():
    """Get all active keywords grouped by category (public endpoint)"""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    
    keywords = db.get_keywords(active_only=True)
    
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
    
    return {"keywords": db.get_keywords(active_only=False)}


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
    
    keyword_id = db.add_keyword(
        name=keyword_request.name,
        search_term=keyword_request.search_term,
        category=keyword_request.category,
        display_order=keyword_request.display_order,
        is_active=keyword_request.is_active
    )
    
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
    
    success = db.update_keyword(
        keyword_id=keyword_id,
        name=update_request.name,
        search_term=update_request.search_term,
        category=update_request.category,
        display_order=update_request.display_order,
        is_active=update_request.is_active
    )
    
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
    
    # Get keyword name before deletion for logging
    keyword = db.get_keyword(keyword_id)
    keyword_name = keyword["name"] if keyword else f"ID {keyword_id}"
    
    success = db.delete_keyword(keyword_id)
    
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
    
    # Perform the recount
    counts = db.update_keyword_counts()
    
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
    
    count = db.seed_default_keywords()
    
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
    
    # Get manifest stats
    manifest_stats = db.get_manifest_stats()
    
    # Get missing documents stats
    missing_stats = db.get_missing_documents_stats()
    
    return {
        "manifest": manifest_stats,
        "missing": missing_stats
    }


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
    
    missing_docs = db.get_missing_documents(dataset_num=dataset)
    
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
    
    manifest = db.get_manifest(dataset_num=dataset, status=status)
    
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
    
    not_downloaded = db.get_not_downloaded(dataset_num=dataset)
    
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
    
    success = db.remove_missing_document(filename, dataset)
    
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


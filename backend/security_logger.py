"""
Security Logging Module for Epstein Files Search Platform
Provides comprehensive logging for security, access, and audit purposes.
Includes rate limiting, session management, and bot protection for all endpoints.
"""

import os
import json
import logging
import traceback
import time
import hmac
import hashlib
import secrets
import base64
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional, Dict, Any, List, Tuple
from uuid import uuid4
from functools import wraps
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

from fastapi import Request, Response, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import Message

# Configuration
BASE_PATH = Path(os.getenv("EPSTEIN_BASE_PATH", Path(__file__).parent.parent))

# Trusted proxy IPs - only trust X-Forwarded-For from these IPs
TRUSTED_PROXIES = set(filter(None, os.getenv("TRUSTED_PROXIES", "").split(",")))

# Cloudflare IP ranges (IPv4) - these are the IPs Cloudflare uses to connect to origin servers
# Updated from: https://www.cloudflare.com/ips-v4
CLOUDFLARE_IP_RANGES = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
]

# Enable Cloudflare mode (auto-detect CF-Connecting-IP header)
CLOUDFLARE_MODE = os.getenv("CLOUDFLARE_MODE", "auto").lower()  # "auto", "enabled", "disabled"

def _ip_in_cloudflare_range(ip: str) -> bool:
    """Check if an IP is in Cloudflare's IP ranges"""
    import ipaddress
    try:
        ip_obj = ipaddress.ip_address(ip)
        for cidr in CLOUDFLARE_IP_RANGES:
            if ip_obj in ipaddress.ip_network(cidr):
                return True
    except ValueError:
        pass
    return False

LOG_DIR = BASE_PATH / "logs"
try:
    LOG_DIR.mkdir(exist_ok=True)
except (PermissionError, OSError):
    pass  # Use fallback logging if we can't create or write to logs dir

# Log file paths
ACCESS_LOG = LOG_DIR / "access.log"
SECURITY_LOG = LOG_DIR / "security.log"
ERROR_LOG = LOG_DIR / "error.log"
AUDIT_LOG = LOG_DIR / "audit.log"

# Log retention configuration
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB per file
BACKUP_COUNT = 30  # Keep 30 rotated files


# =============================================================================
# SESSION MANAGEMENT CONFIGURATION
# =============================================================================

# Generate or load session secret key (should be in environment in production)
SESSION_SECRET_KEY = os.getenv(
    "SESSION_SECRET_KEY", 
    secrets.token_hex(32)  # 256-bit key
)

# Whether to mark session cookies as Secure (requires HTTPS)
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"

# Session configuration
SESSION_COOKIE_NAME = "epstein_session"
SESSION_MAX_AGE = 86400 * 7  # 7 days
SESSION_REFRESH_AFTER = 3600  # Refresh session after 1 hour

# Blocked sessions (for session-based blocking)
BLOCKED_SESSIONS: set = set()

# Session violation tracking (session_id -> violation timestamps)
SESSION_VIOLATIONS: Dict[str, List[float]] = defaultdict(list)


@dataclass
class Session:
    """Represents a client session"""
    session_id: str
    created_at: float
    last_seen: float
    ip_address: str
    user_agent_hash: str
    fingerprint: str  # Combined hash of multiple factors
    is_valid: bool = True
    
    def to_cookie_value(self) -> str:
        """Serialize session to signed cookie value"""
        data = {
            "sid": self.session_id,
            "created": self.created_at,
            "ip": self.ip_address[:20],  # Truncate for privacy
            "uah": self.user_agent_hash[:16],
            "fp": self.fingerprint[:16]
        }
        payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        signature = SessionManager.sign(payload)
        return f"{payload}.{signature}"
    
    @classmethod
    def from_cookie_value(cls, cookie_value: str, current_ip: str, current_ua_hash: str) -> Optional['Session']:
        """Deserialize and validate session from cookie"""
        try:
            # Strip any surrounding quotes that browsers/curl might add
            cookie_value = cookie_value.strip('"\'')
            
            parts = cookie_value.split(".")
            if len(parts) != 2:
                return None
            
            payload, signature = parts
            
            # Verify signature
            if not SessionManager.verify(payload, signature):
                return None
            
            # Decode payload (handle both padded and unpadded base64)
            # Add padding if needed
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            
            data = json.loads(base64.urlsafe_b64decode(payload.encode()))
            
            session = cls(
                session_id=data["sid"],
                created_at=data["created"],
                last_seen=time.time(),
                ip_address=data["ip"],
                user_agent_hash=data["uah"],
                fingerprint=data["fp"],
                is_valid=True
            )
            
            # Validate session hasn't expired
            if time.time() - session.created_at > SESSION_MAX_AGE:
                return None
            
            # Check if session is blocked
            if session.session_id in BLOCKED_SESSIONS:
                return None
            
            return session
            
        except (json.JSONDecodeError, KeyError, ValueError, Exception):
            return None


class SessionManager:
    """Manages session creation, validation, and security"""
    
    _instance = None
    _lock = Lock()
    
    # Track active sessions for monitoring
    _active_sessions: Dict[str, Session] = {}
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    @staticmethod
    def sign(payload: str) -> str:
        """Create HMAC signature for payload"""
        return hmac.new(
            SESSION_SECRET_KEY.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()[:32]
    
    @staticmethod
    def verify(payload: str, signature: str) -> bool:
        """Verify HMAC signature"""
        expected = SessionManager.sign(payload)
        return hmac.compare_digest(expected, signature)
    
    @staticmethod
    def hash_user_agent(user_agent: str) -> str:
        """Create a hash of the user agent for fingerprinting"""
        return hashlib.sha256(user_agent.encode()).hexdigest()[:16]
    
    @staticmethod
    def create_fingerprint(ip: str, user_agent: str, accept_lang: str, accept_enc: str) -> str:
        """
        Create a browser fingerprint from multiple request attributes.
        This makes it harder to spoof sessions across different browsers/devices.
        Uses IP prefix (first 2 octets) to allow for NAT/CGNAT variation.
        """
        # Use IP prefix (first 3 octets for IPv4, first 4 groups for IPv6)
        if "." in ip:
            ip_prefix = ".".join(ip.split(".")[:3])  # e.g., "192.168.1"
        else:
            # IPv6 - use first 4 groups
            ip_prefix = ":".join(ip.split(":")[:4])
        
        components = [
            ip_prefix,
            user_agent[:100],
            accept_lang[:50] if accept_lang else "",
            accept_enc[:50] if accept_enc else ""
        ]
        combined = "|".join(components)
        return hashlib.sha256(combined.encode()).hexdigest()[:24]
    
    def create_session(
        self,
        ip_address: str,
        user_agent: str,
        accept_lang: str = "",
        accept_enc: str = ""
    ) -> Session:
        """Create a new session"""
        now = time.time()
        session_id = secrets.token_urlsafe(24)  # 192-bit random ID
        ua_hash = self.hash_user_agent(user_agent)
        fingerprint = self.create_fingerprint(ip_address, user_agent, accept_lang, accept_enc)
        
        session = Session(
            session_id=session_id,
            created_at=now,
            last_seen=now,
            ip_address=ip_address[:20],
            user_agent_hash=ua_hash,
            fingerprint=fingerprint
        )
        
        self._active_sessions[session_id] = session
        return session
    
    def validate_session(
        self,
        session: Session,
        current_ip: str,
        current_ua: str,
        current_accept_lang: str = "",
        current_accept_enc: str = ""
    ) -> Tuple[bool, str]:
        """
        Validate session integrity. Returns (is_valid, reason).
        Checks for session hijacking attempts.
        """
        current_ua_hash = self.hash_user_agent(current_ua)
        current_fp = self.create_fingerprint(
            current_ip, current_ua, current_accept_lang, current_accept_enc
        )
        
        # Check if session is blocked
        if session.session_id in BLOCKED_SESSIONS:
            return False, "session_blocked"
        
        # Check fingerprint match - compare only stored length
        # Session stores 16 chars, we compute 24
        stored_fp_len = len(session.fingerprint)
        if session.fingerprint != current_fp[:stored_fp_len]:
            # Fingerprint mismatch could indicate session hijacking
            return False, "fingerprint_mismatch"
        
        # Check user agent hash - compare only stored length
        stored_ua_len = len(session.user_agent_hash)
        if session.user_agent_hash != current_ua_hash[:stored_ua_len]:
            return False, "user_agent_changed"
        
        return True, "valid"
    
    def get_rate_limit_key(self, session: Optional[Session], ip: str) -> str:
        """
        Generate a rate limit key that combines session and IP.
        This prevents bypassing limits by either method alone.
        
        Strategy: Track both session-based and IP-based limits.
        The middleware applies BOTH, so abuse on either vector is caught.
        """
        if session and session.is_valid:
            # Use session fingerprint + session ID for strong tracking
            # This ties the rate limit to the browser fingerprint
            return f"sess:{session.fingerprint}:{session.session_id[:8]}"
        else:
            # Fallback to IP for requests without valid session
            # (these are already suspicious)
            return f"ip:{ip}"
    
    def block_session(self, session_id: str):
        """Block a session"""
        BLOCKED_SESSIONS.add(session_id)
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
    
    def record_session_violation(self, session_id: str) -> bool:
        """Record a violation for a session, return True if should be blocked"""
        now = time.time()
        cutoff = now - AUTO_BLOCK_WINDOW
        
        # Clean old violations
        SESSION_VIOLATIONS[session_id] = [
            ts for ts in SESSION_VIOLATIONS[session_id] if ts > cutoff
        ]
        
        # Record new violation
        SESSION_VIOLATIONS[session_id].append(now)
        
        # Check if should block
        if len(SESSION_VIOLATIONS[session_id]) >= AUTO_BLOCK_THRESHOLD:
            self.block_session(session_id)
            return True
        
        return False
    
    def get_session_stats(self) -> Dict[str, Any]:
        """Get session management statistics"""
        return {
            "active_sessions": len(self._active_sessions),
            "blocked_sessions": len(BLOCKED_SESSIONS),
            "sessions_with_violations": len(SESSION_VIOLATIONS)
        }


def get_session_manager() -> SessionManager:
    """Get the singleton session manager instance"""
    return SessionManager()


# =============================================================================
# RATE LIMITING CONFIGURATION
# =============================================================================

@dataclass
class RateLimitConfig:
    """Configuration for a rate limit rule"""
    requests: int          # Max requests allowed
    window_seconds: int    # Time window in seconds
    burst: int = 0         # Additional burst allowance (0 = no burst)
    
    def __post_init__(self):
        if self.burst == 0:
            self.burst = self.requests


# Rate limit configurations per endpoint pattern
# Format: path_pattern -> RateLimitConfig
RATE_LIMITS: Dict[str, RateLimitConfig] = {
    # LLM endpoints - most expensive, strictest limits
    "/api/ask": RateLimitConfig(requests=10, window_seconds=60, burst=15),
    "/api/ask/stream": RateLimitConfig(requests=10, window_seconds=60, burst=15),
    "/api/documents/*/summary": RateLimitConfig(requests=10, window_seconds=60, burst=15),
    
    # Search endpoints - moderate limits
    "/api/search": RateLimitConfig(requests=30, window_seconds=60, burst=50),
    
    # Document file downloads - tightened to prevent scraping
    "/api/documents/*/file": RateLimitConfig(requests=50, window_seconds=60, burst=60),
    
    # Index trigger - very restricted (admin action)
    "/api/index/trigger": RateLimitConfig(requests=2, window_seconds=300, burst=3),
    
    # Feedback - already has its own rate limiting, but add global protection
    "/api/feedback": RateLimitConfig(requests=5, window_seconds=60, burst=10),
    
    # Stats/categories - allow more frequent access
    "/api/stats": RateLimitConfig(requests=60, window_seconds=60, burst=100),
    "/api/categories": RateLimitConfig(requests=60, window_seconds=60, burst=100),
    
    # Global fallback for any unmatched API endpoint
    "/api/*": RateLimitConfig(requests=100, window_seconds=60, burst=150),
}

# Global rate limit across all endpoints per IP
GLOBAL_RATE_LIMIT = RateLimitConfig(requests=300, window_seconds=60, burst=500)

# IPs to exempt from rate limiting (e.g., monitoring, internal services)
RATE_LIMIT_EXEMPT_IPS: set = {"127.0.0.1", "::1"}

# Blocked IPs (manually blocked or auto-blocked for severe violations)
BLOCKED_IPS: set = set()

# Track IPs that repeatedly hit rate limits for potential auto-blocking
RATE_LIMIT_VIOLATIONS: Dict[str, List[float]] = defaultdict(list)
AUTO_BLOCK_THRESHOLD = 50  # Block after 50 violations in the window
AUTO_BLOCK_WINDOW = 300    # 5 minute window for counting violations


class SlidingWindowRateLimiter:
    """
    Thread-safe sliding window rate limiter with burst support.
    Uses a sliding log algorithm for accurate rate limiting.
    """
    
    def __init__(self):
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = Lock()
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # Cleanup old entries every 60 seconds
    
    def _cleanup_old_entries(self, now: float):
        """Remove entries older than the maximum window we track"""
        max_window = max(
            config.window_seconds 
            for config in RATE_LIMITS.values()
        )
        max_window = max(max_window, GLOBAL_RATE_LIMIT.window_seconds, AUTO_BLOCK_WINDOW)
        cutoff = now - max_window - 60  # Add buffer
        
        keys_to_remove = []
        for key, timestamps in self._requests.items():
            # Filter out old timestamps
            self._requests[key] = [ts for ts in timestamps if ts > cutoff]
            if not self._requests[key]:
                keys_to_remove.append(key)
        
        # Remove empty keys
        for key in keys_to_remove:
            del self._requests[key]
    
    def _get_key(self, client_ip: str, endpoint: str) -> str:
        """Generate a unique key for IP + endpoint combination"""
        return f"{client_ip}:{endpoint}"
    
    def _match_endpoint(self, path: str) -> Tuple[str, RateLimitConfig]:
        """
        Match a request path to a rate limit configuration.
        Supports wildcards (*) in path patterns.
        Returns (matched_pattern, config)
        """
        # First try exact match
        if path in RATE_LIMITS:
            return path, RATE_LIMITS[path]
        
        # Try wildcard matches (most specific first)
        for pattern, config in sorted(RATE_LIMITS.items(), key=lambda x: -len(x[0])):
            if "*" in pattern:
                # Convert pattern to regex-like matching
                parts = pattern.split("*")
                if len(parts) == 2:
                    prefix, suffix = parts
                    if path.startswith(prefix) and path.endswith(suffix):
                        return pattern, config
        
        # Fallback to global API limit if it's an API endpoint
        if path.startswith("/api/"):
            return "/api/*", RATE_LIMITS.get("/api/*", GLOBAL_RATE_LIMIT)
        
        # No specific limit for non-API endpoints (static files, etc.)
        return None, None
    
    def is_allowed(
        self, 
        client_ip: str, 
        path: str,
        session: Optional[Session] = None
    ) -> Tuple[bool, Optional[str], Optional[int], Optional[int]]:
        """
        Check if a request is allowed under rate limits.
        Uses session + IP combination for rate limiting to prevent bypasses.
        
        Returns:
            (allowed, matched_pattern, remaining_requests, retry_after_seconds)
        """
        now = time.time()
        
        # Check if IP is blocked
        if client_ip in BLOCKED_IPS:
            return False, "blocked", 0, 3600
        
        # Check if session is blocked
        if session and session.session_id in BLOCKED_SESSIONS:
            return False, "session_blocked", 0, 3600
        
        # Check if IP is exempt (only for truly local requests)
        if client_ip in RATE_LIMIT_EXEMPT_IPS:
            return True, "exempt", -1, None
        
        # Generate rate limit key based on session + IP
        session_manager = get_session_manager()
        rate_key = session_manager.get_rate_limit_key(session, client_ip)
        
        with self._lock:
            # Periodic cleanup
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_old_entries(now)
                self._last_cleanup = now
            
            # Match endpoint to rate limit config
            pattern, config = self._match_endpoint(path)
            
            # No rate limit for this path (non-API)
            if config is None:
                return True, None, -1, None
            
            # === DUAL TRACKING: Check both session-based and IP-based limits ===
            # This prevents bypass via either method
            
            window_start = now - config.window_seconds
            
            # 1. Check session-based limit
            session_key = f"{rate_key}:{pattern}"
            session_timestamps = self._requests[session_key]
            session_recent = [ts for ts in session_timestamps if ts > window_start]
            
            if len(session_recent) >= config.burst:
                oldest = min(session_recent) if session_recent else now
                retry_after = int(oldest + config.window_seconds - now) + 1
                return False, pattern, 0, max(1, retry_after)
            
            # 2. Also check IP-based limit (prevents IP-hopping with cleared cookies)
            ip_key = f"ip:{client_ip}:{pattern}"
            ip_timestamps = self._requests[ip_key]
            ip_recent = [ts for ts in ip_timestamps if ts > window_start]
            
            if len(ip_recent) >= config.burst:
                oldest = min(ip_recent) if ip_recent else now
                retry_after = int(oldest + config.window_seconds - now) + 1
                return False, f"ip:{pattern}", 0, max(1, retry_after)
            
            # 3. Check global limits (both session and IP)
            global_window_start = now - GLOBAL_RATE_LIMIT.window_seconds
            
            session_global_key = f"{rate_key}:global"
            session_global = self._requests[session_global_key]
            session_global_recent = [ts for ts in session_global if ts > global_window_start]
            
            if len(session_global_recent) >= GLOBAL_RATE_LIMIT.burst:
                oldest = min(session_global_recent) if session_global_recent else now
                retry_after = int(oldest + GLOBAL_RATE_LIMIT.window_seconds - now) + 1
                return False, "global", 0, max(1, retry_after)
            
            ip_global_key = f"ip:{client_ip}:global"
            ip_global = self._requests[ip_global_key]
            ip_global_recent = [ts for ts in ip_global if ts > global_window_start]
            
            if len(ip_global_recent) >= GLOBAL_RATE_LIMIT.burst:
                oldest = min(ip_global_recent) if ip_global_recent else now
                retry_after = int(oldest + GLOBAL_RATE_LIMIT.window_seconds - now) + 1
                return False, "ip:global", 0, max(1, retry_after)
            
            # Request is allowed - record to BOTH tracking methods
            self._requests[session_key].append(now)
            self._requests[ip_key].append(now)
            self._requests[session_global_key].append(now)
            self._requests[ip_global_key].append(now)
            
            # Return the more restrictive remaining count
            remaining = min(
                config.requests - len(session_recent) - 1,
                config.requests - len(ip_recent) - 1
            )
            return True, pattern, max(0, remaining), None
    
    def record_violation(self, client_ip: str, session: Optional[Session] = None) -> Tuple[bool, bool]:
        """
        Record a rate limit violation for potential auto-blocking.
        Returns (ip_blocked, session_blocked)
        """
        now = time.time()
        ip_blocked = False
        session_blocked = False
        
        with self._lock:
            # Clean old violations
            cutoff = now - AUTO_BLOCK_WINDOW
            RATE_LIMIT_VIOLATIONS[client_ip] = [
                ts for ts in RATE_LIMIT_VIOLATIONS[client_ip] if ts > cutoff
            ]
            
            # Record new violation for IP
            RATE_LIMIT_VIOLATIONS[client_ip].append(now)
            
            # Check if should auto-block IP
            if len(RATE_LIMIT_VIOLATIONS[client_ip]) >= AUTO_BLOCK_THRESHOLD:
                BLOCKED_IPS.add(client_ip)
                ip_blocked = True
        
        # Also record violation for session
        if session and session.session_id:
            session_manager = get_session_manager()
            session_blocked = session_manager.record_session_violation(session.session_id)
        
        return ip_blocked, session_blocked
    
    def get_stats(self, client_ip: str) -> Dict[str, Any]:
        """Get rate limit stats for an IP"""
        now = time.time()
        stats = {}
        
        with self._lock:
            for pattern, config in RATE_LIMITS.items():
                key = self._get_key(client_ip, pattern)
                timestamps = self._requests.get(key, [])
                window_start = now - config.window_seconds
                recent = [ts for ts in timestamps if ts > window_start]
                
                stats[pattern] = {
                    "used": len(recent),
                    "limit": config.requests,
                    "burst": config.burst,
                    "remaining": max(0, config.requests - len(recent)),
                    "window_seconds": config.window_seconds
                }
        
        return stats


# Global rate limiter instance
_rate_limiter: Optional[SlidingWindowRateLimiter] = None


def get_rate_limiter() -> SlidingWindowRateLimiter:
    """Get the singleton rate limiter instance"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = SlidingWindowRateLimiter()
    return _rate_limiter


class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging"""
    
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add extra fields if present
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": traceback.format_exception(*record.exc_info)
            }
        
        return json.dumps(log_entry, default=str)


class SecurityLogger:
    """Central security logging facility"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._setup_loggers()
    
    def _setup_loggers(self):
        """Configure all security-related loggers"""
        
        # Access Logger - All HTTP requests
        self.access_logger = self._create_logger(
            name="epstein.access",
            log_file=ACCESS_LOG,
            level=logging.INFO
        )
        
        # Security Logger - Security events (auth, rate limits, suspicious activity)
        self.security_logger = self._create_logger(
            name="epstein.security",
            log_file=SECURITY_LOG,
            level=logging.INFO
        )
        
        # Error Logger - Application errors
        self.error_logger = self._create_logger(
            name="epstein.error",
            log_file=ERROR_LOG,
            level=logging.ERROR
        )
        
        # Audit Logger - Document access, file downloads, admin actions
        self.audit_logger = self._create_logger(
            name="epstein.audit",
            log_file=AUDIT_LOG,
            level=logging.INFO
        )
        
        # Console logger for all events
        self.console_logger = logging.getLogger("epstein.console")
        self.console_logger.setLevel(logging.INFO)
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        self.console_logger.addHandler(console_handler)
    
    def _create_logger(
        self, 
        name: str, 
        log_file: Path, 
        level: int = logging.INFO
    ) -> logging.Logger:
        """Create a logger with rotating file handler and JSON formatting.
        On permission errors (e.g. after deploy when logs dir is root-owned),
        falls back to stderr so the app still starts.
        """
        import sys
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False

        try:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=MAX_LOG_SIZE,
                backupCount=BACKUP_COUNT,
                encoding="utf-8"
            )
            file_handler.setFormatter(JSONFormatter())
            logger.addHandler(file_handler)
        except (PermissionError, OSError):
            # After deploy, logs dir may be root-owned; don't crash - log to stderr
            fallback = logging.StreamHandler(sys.stderr)
            fallback.setFormatter(JSONFormatter())
            logger.addHandler(fallback)

        return logger
    
    def _log_with_extra(
        self, 
        logger: logging.Logger, 
        level: int, 
        message: str, 
        **kwargs
    ):
        """Log a message with extra structured fields"""
        record = logger.makeRecord(
            logger.name,
            level,
            "",
            0,
            message,
            (),
            None
        )
        record.extra_fields = kwargs
        logger.handle(record)
    
    # === Access Logging ===
    
    def log_request(
        self,
        request_id: str,
        method: str,
        path: str,
        client_ip: str,
        user_agent: str,
        status_code: int,
        duration_ms: float,
        query_params: Optional[Dict] = None,
        response_size: Optional[int] = None,
        **extra
    ):
        """Log an HTTP request"""
        message = f"{method} {path} {status_code} {duration_ms:.2f}ms"
        
        self._log_with_extra(
            self.access_logger,
            logging.INFO,
            message,
            request_id=request_id,
            method=method,
            path=path,
            client_ip=client_ip,
            user_agent=user_agent[:200] if user_agent else None,
            status_code=status_code,
            duration_ms=round(duration_ms, 2),
            query_params=query_params,
            response_size=response_size,
            event_type="http_request",
            **extra
        )
        
        # Console output for visibility
        status_emoji = "✓" if status_code < 400 else "✗" if status_code >= 500 else "⚠"
        self.console_logger.info(
            f"{status_emoji} {client_ip} | {method} {path} | {status_code} | {duration_ms:.0f}ms"
        )
    
    # === Security Event Logging ===
    
    def log_security_event(
        self,
        event_type: str,
        severity: str,
        client_ip: str,
        message: str,
        request_id: Optional[str] = None,
        **details
    ):
        """Log a security-related event"""
        level = {
            "critical": logging.CRITICAL,
            "high": logging.ERROR,
            "medium": logging.WARNING,
            "low": logging.INFO
        }.get(severity.lower(), logging.INFO)
        
        self._log_with_extra(
            self.security_logger,
            level,
            message,
            event_type=event_type,
            severity=severity,
            client_ip=client_ip,
            request_id=request_id,
            **details
        )
        
        # Console output with color coding
        severity_emoji = {
            "critical": "🚨",
            "high": "🔴",
            "medium": "🟡",
            "low": "🔵"
        }.get(severity.lower(), "ℹ️")
        
        self.console_logger.warning(
            f"{severity_emoji} SECURITY | {event_type} | {client_ip} | {message}"
        )
    
    def log_rate_limit_exceeded(
        self,
        client_ip: str,
        endpoint: str,
        limit: int,
        window_seconds: int,
        request_id: Optional[str] = None
    ):
        """Log rate limit violation"""
        self.log_security_event(
            event_type="rate_limit_exceeded",
            severity="medium",
            client_ip=client_ip,
            message=f"Rate limit exceeded for {endpoint}",
            request_id=request_id,
            endpoint=endpoint,
            limit=limit,
            window_seconds=window_seconds
        )
    
    def log_recaptcha_failure(
        self,
        client_ip: str,
        reason: str,
        score: Optional[float] = None,
        request_id: Optional[str] = None
    ):
        """Log reCAPTCHA verification failure"""
        self.log_security_event(
            event_type="recaptcha_failure",
            severity="medium",
            client_ip=client_ip,
            message=f"reCAPTCHA verification failed: {reason}",
            request_id=request_id,
            score=score,
            failure_reason=reason
        )
    
    def log_suspicious_activity(
        self,
        client_ip: str,
        activity_type: str,
        description: str,
        request_id: Optional[str] = None,
        **details
    ):
        """Log suspicious activity detection"""
        self.log_security_event(
            event_type="suspicious_activity",
            severity="high",
            client_ip=client_ip,
            message=f"Suspicious activity detected: {activity_type}",
            request_id=request_id,
            activity_type=activity_type,
            description=description,
            **details
        )
    
    def log_validation_failure(
        self,
        client_ip: str,
        endpoint: str,
        field: str,
        reason: str,
        request_id: Optional[str] = None
    ):
        """Log input validation failures"""
        self.log_security_event(
            event_type="validation_failure",
            severity="low",
            client_ip=client_ip,
            message=f"Validation failed for {field}: {reason}",
            request_id=request_id,
            endpoint=endpoint,
            field=field,
            reason=reason
        )
    
    # === Audit Logging ===
    
    def log_document_access(
        self,
        client_ip: str,
        document_id: str,
        document_path: str,
        action: str,  # "view", "download", "search_result"
        request_id: Optional[str] = None,
        **details
    ):
        """Log document access for audit trail"""
        message = f"Document {action}: {document_id}"
        
        self._log_with_extra(
            self.audit_logger,
            logging.INFO,
            message,
            event_type="document_access",
            client_ip=client_ip,
            document_id=document_id,
            document_path=document_path,
            action=action,
            request_id=request_id,
            **details
        )
        
        self.console_logger.info(
            f"📄 AUDIT | {action.upper()} | {client_ip} | {document_id}"
        )
    
    def log_search_query(
        self,
        client_ip: str,
        query: str,
        search_type: str,
        result_count: int,
        request_id: Optional[str] = None,
        **details
    ):
        """Log search queries for audit and analytics"""
        # Truncate query for logging (prevent log injection)
        safe_query = query[:500].replace("\n", " ").replace("\r", "") if query else ""
        
        message = f"Search: '{safe_query[:50]}...' ({result_count} results)"
        
        self._log_with_extra(
            self.audit_logger,
            logging.INFO,
            message,
            event_type="search_query",
            client_ip=client_ip,
            query=safe_query,
            search_type=search_type,
            result_count=result_count,
            request_id=request_id,
            **details
        )
    
    def log_llm_query(
        self,
        client_ip: str,
        question: str,
        context_docs_count: int,
        request_id: Optional[str] = None,
        **details
    ):
        """Log LLM/AI queries"""
        safe_question = question[:500].replace("\n", " ").replace("\r", "") if question else ""
        
        self._log_with_extra(
            self.audit_logger,
            logging.INFO,
            f"LLM Query: '{safe_question[:50]}...'",
            event_type="llm_query",
            client_ip=client_ip,
            question=safe_question,
            context_docs_count=context_docs_count,
            request_id=request_id,
            **details
        )
    
    def log_feedback_submission(
        self,
        client_ip: str,
        feedback_type: str,
        feedback_id: str,
        request_id: Optional[str] = None,
        **details
    ):
        """Log feedback submissions"""
        self._log_with_extra(
            self.audit_logger,
            logging.INFO,
            f"Feedback submitted: {feedback_type}",
            event_type="feedback_submission",
            client_ip=client_ip,
            feedback_type=feedback_type,
            feedback_id=feedback_id,
            request_id=request_id,
            **details
        )
    
    # === Error Logging ===
    
    def log_error(
        self,
        error: Exception,
        context: str,
        client_ip: Optional[str] = None,
        request_id: Optional[str] = None,
        **details
    ):
        """Log application errors with full context"""
        self._log_with_extra(
            self.error_logger,
            logging.ERROR,
            f"Error in {context}: {str(error)}",
            event_type="application_error",
            error_type=type(error).__name__,
            error_message=str(error),
            context=context,
            client_ip=client_ip,
            request_id=request_id,
            traceback=traceback.format_exc(),
            **details
        )
        
        self.console_logger.error(f"❌ ERROR | {context} | {type(error).__name__}: {str(error)}")
    
    # === System Event Logging ===
    
    def log_system_event(
        self,
        event_type: str,
        message: str,
        **details
    ):
        """Log system-level events (startup, shutdown, indexing, etc.)"""
        self._log_with_extra(
            self.security_logger,
            logging.INFO,
            message,
            event_type=f"system_{event_type}",
            **details
        )
        
        self.console_logger.info(f"⚙️  SYSTEM | {event_type} | {message}")
    
    def log_index_operation(
        self,
        operation: str,  # "start", "complete", "error"
        trigger: str,    # "manual", "auto", "startup"
        duration_seconds: Optional[float] = None,
        document_count: Optional[int] = None,
        **details
    ):
        """Log indexing operations"""
        message = f"Index {operation}: {trigger} trigger"
        if document_count:
            message += f", {document_count} documents"
        
        self._log_with_extra(
            self.audit_logger,
            logging.INFO,
            message,
            event_type="index_operation",
            operation=operation,
            trigger=trigger,
            duration_seconds=duration_seconds,
            document_count=document_count,
            **details
        )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log all HTTP requests with:
    - Session-based rate limiting (prevents header spoofing bypasses)
    - Timing and security checks
    - Browser fingerprinting
    """
    
    def __init__(self, app, security_logger: SecurityLogger):
        super().__init__(app)
        self.security_logger = security_logger
        self.rate_limiter = get_rate_limiter()
        self.session_manager = get_session_manager()
    
    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate unique request ID for correlation
        request_id = str(uuid4())[:8]
        request.state.request_id = request_id
        
        # Extract client IP (handle proxies)
        client_ip = self._get_client_ip(request)
        request.state.client_ip = client_ip
        
        path = str(request.url.path)
        user_agent = request.headers.get("user-agent", "")
        accept_lang = request.headers.get("accept-language", "")
        accept_enc = request.headers.get("accept-encoding", "")
        
        # Start timing
        start_time = datetime.utcnow()
        
        # === SESSION HANDLING ===
        session = None
        new_session_created = False
        session_validation_failed = False
        
        # Check for existing session cookie
        session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        
        if session_cookie:
            # Try to validate existing session
            ua_hash = self.session_manager.hash_user_agent(user_agent)
            session = Session.from_cookie_value(session_cookie, client_ip, ua_hash)
            
            if session:
                # Validate session integrity (check for hijacking)
                is_valid, reason = self.session_manager.validate_session(
                    session, client_ip, user_agent, accept_lang, accept_enc
                )
                
                if not is_valid:
                    # Log the validation failure
                    self.security_logger.log_security_event(
                        event_type="session_validation_failed",
                        severity="high",
                        client_ip=client_ip,
                        message=f"Session validation failed: {reason}",
                        request_id=request_id,
                        session_id=session.session_id[:8] + "...",
                        reason=reason
                    )
                    session_validation_failed = True
                    session = None  # Invalidate the session
        
        # Create new session if needed (for API requests without valid session)
        if session is None and path.startswith("/api/"):
            session = self.session_manager.create_session(
                client_ip, user_agent, accept_lang, accept_enc
            )
            new_session_created = True
            
            self.security_logger.log_security_event(
                event_type="session_created",
                severity="low",
                client_ip=client_ip,
                message="New session created",
                request_id=request_id,
                session_id=session.session_id[:8] + "..."
            )
        
        # Store session in request state for endpoint access
        request.state.session = session
        
        # === RATE LIMITING CHECK (now using session) ===
        allowed, pattern, remaining, retry_after = self.rate_limiter.is_allowed(
            client_ip, path, session
        )
        
        if not allowed:
            # Record the violation (for both IP and session)
            ip_blocked, session_blocked = self.rate_limiter.record_violation(client_ip, session)
            
            # Log the rate limit event
            if pattern == "blocked":
                self.security_logger.log_security_event(
                    event_type="blocked_ip_access",
                    severity="high",
                    client_ip=client_ip,
                    message=f"Blocked IP attempted access: {path}",
                    request_id=request_id,
                    path=path
                )
            elif pattern == "session_blocked":
                self.security_logger.log_security_event(
                    event_type="blocked_session_access",
                    severity="high",
                    client_ip=client_ip,
                    message=f"Blocked session attempted access: {path}",
                    request_id=request_id,
                    path=path,
                    session_id=session.session_id[:8] + "..." if session else None
                )
            else:
                limit_config = RATE_LIMITS.get(pattern, GLOBAL_RATE_LIMIT)
                self.security_logger.log_rate_limit_exceeded(
                    client_ip=client_ip,
                    endpoint=pattern or path,
                    limit=limit_config.requests if limit_config else 0,
                    window_seconds=limit_config.window_seconds if limit_config else 60,
                    request_id=request_id
                )
                
                if ip_blocked:
                    self.security_logger.log_security_event(
                        event_type="ip_auto_blocked",
                        severity="critical",
                        client_ip=client_ip,
                        message=f"IP auto-blocked after {AUTO_BLOCK_THRESHOLD} rate limit violations",
                        request_id=request_id
                    )
                
                if session_blocked:
                    self.security_logger.log_security_event(
                        event_type="session_auto_blocked",
                        severity="critical",
                        client_ip=client_ip,
                        message=f"Session auto-blocked after {AUTO_BLOCK_THRESHOLD} rate limit violations",
                        request_id=request_id,
                        session_id=session.session_id[:8] + "..." if session else None
                    )
            
            # Return 429 Too Many Requests
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please slow down.",
                    "retry_after": retry_after
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Remaining": "0",
                    "X-Request-ID": request_id
                }
            )
            
            # Log the blocked request
            duration = (datetime.utcnow() - start_time).total_seconds() * 1000
            self.security_logger.log_request(
                request_id=request_id,
                method=request.method,
                path=path,
                client_ip=client_ip,
                user_agent=user_agent,
                status_code=429,
                duration_ms=duration,
                rate_limited=True,
                rate_limit_pattern=pattern,
                session_id=session.session_id[:8] + "..." if session else None
            )
            
            return response
        
        # === PROCESS REQUEST ===
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            # Log unhandled errors
            self.security_logger.log_error(
                error=e,
                context="request_processing",
                client_ip=client_ip,
                request_id=request_id,
                path=path,
                method=request.method
            )
            raise
        
        # Calculate duration
        duration = (datetime.utcnow() - start_time).total_seconds() * 1000
        
        # Extract response size if available
        response_size = None
        if hasattr(response, "headers"):
            content_length = response.headers.get("content-length")
            if content_length:
                response_size = int(content_length)
        
        # Log the request
        self.security_logger.log_request(
            request_id=request_id,
            method=request.method,
            path=path,
            client_ip=client_ip,
            user_agent=user_agent,
            status_code=status_code,
            duration_ms=duration,
            query_params=dict(request.query_params) if request.query_params else None,
            response_size=response_size,
            referer=request.headers.get("referer", "")[:200] if request.headers.get("referer") else None,
            session_id=session.session_id[:8] + "..." if session else None
        )
        
        # === SET SESSION COOKIE ===
        if session and (new_session_created or session_validation_failed):
            # Set or refresh the session cookie
            cookie_value = session.to_cookie_value()
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=cookie_value,
                max_age=SESSION_MAX_AGE,
                httponly=True,                  # Prevent JavaScript access
                secure=SESSION_COOKIE_SECURE,   # Set SESSION_COOKIE_SECURE=true in production
                samesite="lax",                 # CSRF protection
                path="/"
            )
        
        # Add rate limit headers to response
        response.headers["X-Request-ID"] = request_id
        if remaining is not None and remaining >= 0:
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        
        # Detect and log suspicious patterns
        self._check_suspicious_patterns(request, client_ip, request_id)
        
        return response
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract the real client IP, handling proxies and Cloudflare securely"""
        # Get the direct connection IP first
        direct_ip = request.client.host if request.client else "unknown"
        
        # Check if request is coming through Cloudflare
        is_cloudflare = False
        if CLOUDFLARE_MODE == "enabled":
            is_cloudflare = True
        elif CLOUDFLARE_MODE == "auto":
            # Auto-detect Cloudflare by checking if direct IP is in CF ranges
            # or if CF-specific headers are present
            is_cloudflare = (
                _ip_in_cloudflare_range(direct_ip) or
                request.headers.get("CF-Connecting-IP") is not None
            )
        
        if is_cloudflare:
            # Cloudflare provides the real client IP in CF-Connecting-IP
            cf_ip = request.headers.get("CF-Connecting-IP")
            if cf_ip:
                return cf_ip.strip()
            
            # Fallback to X-Forwarded-For (CF also sets this)
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        
        # Check if from a trusted proxy (like nginx)
        if direct_ip in TRUSTED_PROXIES:
            # Check X-Forwarded-For header (from reverse proxies)
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                # First IP in the list is the original client
                return forwarded.split(",")[0].strip()
            
            # Check X-Real-IP header
            real_ip = request.headers.get("X-Real-IP")
            if real_ip:
                return real_ip.strip()
        
        return direct_ip
    
    def _check_suspicious_patterns(
        self, 
        request: Request, 
        client_ip: str, 
        request_id: str
    ):
        """Detect and log suspicious request patterns"""
        path = str(request.url.path).lower()
        user_agent = request.headers.get("user-agent", "").lower()
        
        # Skip suspicious pattern checks for authenticated admin API endpoints
        # These are legitimate admin requests that should not be flagged
        if path.startswith("/api/admin/"):
            return
        
        # Common attack patterns
        suspicious_paths = [
            "/admin", "/wp-admin", "/phpmyadmin", "/.env", 
            "/.git", "/config", "/backup", "/shell",
            "/eval", "/exec", "/cmd", "/.htaccess",
            "/etc/passwd", "/etc/shadow"
        ]
        
        suspicious_patterns = [
            "../",  # Path traversal
            "<script",  # XSS attempt
            "union select",  # SQL injection
            "' or '",  # SQL injection
            "1=1",  # SQL injection
            "${",  # Template injection
            "{{",  # Template injection
        ]
        
        # Check for known scanner user agents
        scanner_agents = [
            "sqlmap", "nikto", "nmap", "masscan",
            "burpsuite", "owasp", "acunetix"
        ]
        
        # Check path-based attacks
        for sus_path in suspicious_paths:
            if sus_path in path:
                self.security_logger.log_suspicious_activity(
                    client_ip=client_ip,
                    activity_type="path_probe",
                    description=f"Attempted access to sensitive path: {path}",
                    request_id=request_id,
                    path=path
                )
                return
        
        # Check query string for injection attempts
        query = str(request.url.query).lower()
        full_request = f"{path} {query}"
        
        for pattern in suspicious_patterns:
            if pattern in full_request:
                self.security_logger.log_suspicious_activity(
                    client_ip=client_ip,
                    activity_type="injection_attempt",
                    description=f"Possible injection attempt detected",
                    request_id=request_id,
                    path=path,
                    pattern_matched=pattern
                )
                return
        
        # Check for scanner user agents
        for scanner in scanner_agents:
            if scanner in user_agent:
                self.security_logger.log_suspicious_activity(
                    client_ip=client_ip,
                    activity_type="scanner_detected",
                    description=f"Security scanner detected: {scanner}",
                    request_id=request_id,
                    user_agent=user_agent[:100]
                )
                return


def get_security_logger() -> SecurityLogger:
    """Get the singleton security logger instance"""
    return SecurityLogger()


def get_client_info(request: Request) -> tuple[str, str]:
    """Helper to extract client IP and request ID from request"""
    client_ip = getattr(request.state, "client_ip", "unknown")
    request_id = getattr(request.state, "request_id", None)
    return client_ip, request_id


def block_ip(ip: str) -> bool:
    """Manually block an IP address"""
    BLOCKED_IPS.add(ip)
    get_security_logger().log_security_event(
        event_type="ip_manually_blocked",
        severity="high",
        client_ip=ip,
        message=f"IP manually blocked: {ip}"
    )
    return True


def unblock_ip(ip: str) -> bool:
    """Unblock an IP address"""
    if ip in BLOCKED_IPS:
        BLOCKED_IPS.discard(ip)
        get_security_logger().log_security_event(
            event_type="ip_unblocked",
            severity="medium",
            client_ip=ip,
            message=f"IP unblocked: {ip}"
        )
        return True
    return False


def add_exempt_ip(ip: str) -> bool:
    """Add an IP to the rate limit exempt list"""
    RATE_LIMIT_EXEMPT_IPS.add(ip)
    return True


def get_blocked_ips() -> set:
    """Get set of currently blocked IPs"""
    return BLOCKED_IPS.copy()


def get_rate_limit_stats(client_ip: str) -> Dict[str, Any]:
    """Get rate limit statistics for a specific IP"""
    return get_rate_limiter().get_stats(client_ip)


def block_session(session_id: str) -> bool:
    """Manually block a session"""
    get_session_manager().block_session(session_id)
    get_security_logger().log_security_event(
        event_type="session_manually_blocked",
        severity="high",
        client_ip="admin",
        message=f"Session manually blocked: {session_id[:8]}..."
    )
    return True


def get_session_stats() -> Dict[str, Any]:
    """Get session management statistics"""
    return get_session_manager().get_session_stats()


def get_blocked_sessions() -> set:
    """Get set of currently blocked session IDs"""
    return BLOCKED_SESSIONS.copy()


"""Document file storage backed by Cloudflare R2, with the local disk as a fallback.

The source documents (PDFs, images, audio, video) live in an R2 bucket under the same
relative path the database stores in `documents.path` (e.g. "DOJ Disclosures/Data Set 12/x.pdf").

* Public file views get a 302 to a short-lived presigned R2 URL, so the bytes never leave
  the VM (R2 has no egress fees).
* Features that need the file on disk (in-PDF search, highlighting, thumbnails, text
  re-extraction) call `ensure_local()`, which returns the local file if it exists or
  downloads it into a size-capped cache.
* Admin flows that rewrite a file call `upload()` so R2 stays the source of truth.

Nothing here needs boto3: requests are signed with AWS SigV4 query-string auth (which R2
speaks) and sent with httpx. Everything is off unless R2 credentials are configured.

Environment:
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY   credentials (all required)
    R2_BUCKET            bucket name (default "epsteinfta-docs")
    R2_SERVE             "1" to redirect file views to R2 (default "0": local first)
    R2_CACHE_DIR         local cache for downloaded files (default <base>/.r2cache)
    R2_CACHE_MAX_GB      cache size cap (default 5)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import quote

import httpx

_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID", "").strip()
_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
_BUCKET = os.getenv("R2_BUCKET", "epsteinfta-docs").strip()
_SERVE = os.getenv("R2_SERVE", "0").strip() == "1"
_CACHE_MAX_BYTES = int(float(os.getenv("R2_CACHE_MAX_GB", "5")) * 1024 ** 3)

_REGION = "auto"
_SERVICE = "s3"

# Presigned GET URLs are signed against a time bucket so that every viewer in the same
# window gets the identical URL: browsers and Cloudflare can then reuse cached bytes.
_SIGN_WINDOW_S = 3600
# Cloudflare raises the redirect's browser cache to 4h (Browser Cache TTL), so a link
# must stay valid well past that: at least 11h after it is handed out.
_URL_LIFETIME_S = 12 * 3600

_base_path: Optional[Path] = None
_cache_dir: Optional[Path] = None


def configure(base_path: Path) -> None:
    """Set the directory that document paths are relative to. Call once at startup."""
    global _base_path, _cache_dir
    _base_path = Path(base_path).resolve()
    _cache_dir = Path(os.getenv("R2_CACHE_DIR", str(_base_path / ".r2cache"))).resolve()


def enabled() -> bool:
    return bool(_ACCOUNT_ID and _ACCESS_KEY and _SECRET_KEY and _BUCKET)


def serving_enabled() -> bool:
    """True when public file views should be redirected to R2."""
    return enabled() and _SERVE


def _host() -> str:
    return f"{_ACCOUNT_ID}.r2.cloudflarestorage.com"


def key_for(file_path: Path) -> Optional[str]:
    """Object key for an absolute local path under the base path, or None if outside it."""
    if _base_path is None:
        return None
    try:
        return Path(file_path).resolve().relative_to(_base_path).as_posix()
    except ValueError:
        return None


# ─── SigV4 query-string signing ──────────────────────────────────────────────

def _uri_encode(s: str, safe: str = "-_.~") -> str:
    return quote(s, safe=safe)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def presign_url(method: str, host: str, canonical_uri: str, *, access_key: str, secret_key: str,
                region: str, service: str, when: _dt.datetime, expires: int,
                extra_params: Optional[Dict[str, str]] = None) -> str:
    """Build a SigV4 presigned URL. `canonical_uri` must already be URI-encoded.

    Kept free of module state so it can be checked against AWS's published test vector.
    """
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    datestamp = when.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if extra_params:
        params.update(extra_params)
    query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(params.items()))
    canonical_request = "\n".join([
        method, canonical_uri, query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD",
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{query}&X-Amz-Signature={signature}"


def _object_url(method: str, key: str, *, expires: int = 900,
                when: Optional[_dt.datetime] = None,
                extra_params: Optional[Dict[str, str]] = None) -> str:
    when = when or _dt.datetime.now(_dt.timezone.utc)
    uri = "/" + _uri_encode(_BUCKET) + "/" + _uri_encode(key, safe="-_.~/")
    return presign_url(method, _host(), uri, access_key=_ACCESS_KEY, secret_key=_SECRET_KEY,
                       region=_REGION, service=_SERVICE, when=when, expires=expires,
                       extra_params=extra_params)


def presigned_get(key: str, media_type: str, filename: str = "") -> str:
    """URL a browser can open to view the object inline."""
    now = int(time.time())
    window_start = _dt.datetime.fromtimestamp(now - now % _SIGN_WINDOW_S, _dt.timezone.utc)
    disposition = "inline"
    if filename:
        safe_name = "".join(c for c in filename if c.isprintable() and c not in '"\\')
        disposition = f'inline; filename="{safe_name}"'
    return _object_url("GET", key, expires=_URL_LIFETIME_S, when=window_start, extra_params={
        "response-content-type": media_type,
        "response-content-disposition": disposition,
        "response-cache-control": "private, max-age=3600",
    })


# ─── Object existence / size (cached) ────────────────────────────────────────

_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()
_size_cache: Dict[str, Tuple[Optional[int], float]] = {}
_POSITIVE_TTL = 24 * 3600
_NEGATIVE_TTL = 60


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(timeout=httpx.Timeout(30.0, read=120.0),
                                   limits=httpx.Limits(max_connections=32))
        return _client


def remote_size(key: str) -> Optional[int]:
    """Size in bytes of the object in R2, or None if it is missing or R2 is unreachable."""
    if not enabled():
        return None
    now = time.monotonic()
    hit = _size_cache.get(key)
    if hit is not None:
        size, at = hit
        if now - at < (_POSITIVE_TTL if size is not None else _NEGATIVE_TTL):
            return size
    size: Optional[int] = None
    try:
        resp = _http().head(_object_url("HEAD", key))
        if resp.status_code == 200:
            size = int(resp.headers.get("content-length", "0"))
    except Exception:
        size = None
    if len(_size_cache) > 200_000:
        _size_cache.clear()
    _size_cache[key] = (size, now)
    return size


def forget(key: str) -> None:
    _size_cache.pop(key, None)


# ─── Local copies ────────────────────────────────────────────────────────────

_key_locks: Dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()
_trim_lock = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def _cache_path(key: str) -> Optional[Path]:
    if _cache_dir is None:
        return None
    p = (_cache_dir / key).resolve()
    try:
        p.relative_to(_cache_dir)
    except ValueError:
        return None
    return p


def ensure_local(file_path: Path, max_bytes: Optional[int] = None) -> Optional[Path]:
    """A readable local copy of `file_path`: the file itself if it is on disk, otherwise a
    copy downloaded from R2 into the cache. None if neither exists, or if the remote object
    is larger than `max_bytes`. Blocking: call from a worker thread.
    """
    file_path = Path(file_path)
    if file_path.exists():
        return file_path
    if not enabled():
        return None
    key = key_for(file_path)
    target = _cache_path(key) if key else None
    if not key or target is None:
        return None
    with _lock_for(key):
        if target.exists():
            try:
                os.utime(target)  # mark recently used for the cache trim
            except OSError:
                pass
            return target
        size = remote_size(key)
        if size is None or (max_bytes is not None and size > max_bytes):
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            with _http().stream("GET", _object_url("GET", key)) as resp:
                if resp.status_code != 200:
                    return None
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(1024 * 1024):
                        fh.write(chunk)
            os.replace(tmp, target)
        except Exception:
            return None
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    _trim_cache()
    return target


def restore_local(file_path: Path) -> bool:
    """Make sure `file_path` itself exists on disk, downloading it from R2 into place if
    needed. For admin tools (re-extract, re-download) that derive the document's stored
    path from where the file sits, so a cache path would not do. Blocking. True if the
    file is on disk afterwards."""
    file_path = Path(file_path)
    if file_path.exists():
        return True
    if not enabled():
        return False
    key = key_for(file_path)
    if not key:
        return False
    cached = _cache_path(key)
    if cached is not None and cached.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(cached, file_path)
        return True
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_name(f".{file_path.name}.{uuid.uuid4().hex}.part")
    try:
        with _http().stream("GET", _object_url("GET", key)) as resp:
            if resp.status_code != 200:
                return False
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1024 * 1024):
                    fh.write(chunk)
        os.replace(tmp, file_path)
        return True
    except Exception:
        return False
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _trim_cache() -> None:
    """Delete least-recently-used cached files until the cache is under its cap."""
    if _cache_dir is None or not _cache_dir.exists():
        return
    if not _trim_lock.acquire(blocking=False):
        return
    try:
        files = []
        total = 0
        for root, _dirs, names in os.walk(_cache_dir):
            for name in names:
                p = Path(root) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        if total <= _CACHE_MAX_BYTES:
            return
        files.sort()
        for _mtime, size, p in files:
            if total <= _CACHE_MAX_BYTES * 0.8:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass
    finally:
        _trim_lock.release()


def upload(file_path: Path, media_type: str = "application/octet-stream") -> bool:
    """Copy a local file to R2 under its document key. Blocking. True on success."""
    if not enabled():
        return False
    key = key_for(file_path)
    if not key:
        return False
    try:
        with open(file_path, "rb") as fh:
            data = fh.read()
        resp = _http().put(_object_url("PUT", key), content=data,
                           headers={"content-type": media_type})
        ok = resp.status_code == 200
    except Exception:
        ok = False
    forget(key)
    cached = _cache_path(key)
    if cached is not None and cached.exists():
        try:
            cached.unlink()
        except OSError:
            pass
    return ok

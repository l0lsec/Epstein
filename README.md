# Epstein Files Public Archive

A public-facing search platform for the Epstein document archive. Full-text search, semantic AI search, and LLM-powered document analysis over court records, DOJ disclosures, FOIA releases, and congressional disclosures.

Live instance: **[https://epsteinfta.com](https://epsteinfta.com)**

---

## Content Warning

This archive ingests primary-source documents from federal investigations and civil litigation. Some material is graphic or disturbing, including:

- Allegations of sexual abuse of minors, with associated victim references
- Evidentiary photos, audio, and video from law enforcement productions
- Names, contact information, and travel records appearing in original filings

The platform exposes the documents as released by the source agencies. Many releases include government-applied redactions; **we do not add or remove redactions**. The admin panel (see below) supports hiding individual documents post-ingest when content is identified that should not be served publicly (e.g., victim-identifying material that slipped past upstream redaction). Operators of forks are responsible for reviewing their own deployment and complying with applicable law.

If you operate a public deployment, please honor takedown requests from victims, their counsel, or the source agency.

---

## Features

- **Full-text search** across ~45,000 documents (PDF, image, audio, video) backed by SQLite FTS5 with BM25 ranking
- **Semantic search** via sentence-transformer embeddings and a local vector store
- **Hybrid search** that combines lexical and semantic scores
- **Ask AI** — GPT-powered Q&A over retrieved documents (requires OpenAI key)
- **AI summaries** — per-document on-demand summaries
- **Document browser** with category, subcategory, and file-type filters
- **PDF viewer + extracted text** side-by-side, with original file download
- **Image OCR** for scanned JPG/TIF productions (Tesseract)
- **Audio/video transcription** via Lightning Whisper MLX (Apple Silicon), faster-whisper (CPU), or OpenAI Whisper API
- **CSV export** of search results with optional full text
- **Admin panel** for hide/pin moderation, feedback triage, reclassification, re-extraction, and telemetry
- **Feedback widget** with reCAPTCHA v3 spam protection
- **Maintenance mode** with a live SSE-driven status page
- **Sitemap and OpenGraph metadata** for SEO and social sharing
- **Security/audit logging** with per-IP geolocation, rate limiting, and Cloudflare-aware client IP handling

---

## Document Collection

| Category | Documents | Description | Source |
|----------|-----------|-------------|--------|
| DOJ Disclosures | ~1,004,700 | Evidence files, flight logs, contact books, reports | [justice.gov/epstein/doj-disclosures](https://www.justice.gov/epstein/doj-disclosures) |
| FOIA Files | ~100+ | FBI, CBP, BOP releases | [justice.gov/epstein/foia](https://www.justice.gov/epstein/foia) |
| Court Records | ~12,100 | Legal filings from various cases | [justice.gov/epstein/court-records](https://www.justice.gov/epstein/court-records) |
| House Disclosures | ~18,800 | DOJ-OGR scanned documents (JPG/TIF), video (MP4), audio (WAV) | House Oversight Committee (Google Drive) |

**Total: ~45,700+ documents**

Documents are downloaded from the official sources by scripts under `scripts/` (see below). The repo does not ship the documents themselves.

---

## Quick Start

### 1. Install Dependencies

Python 3.10–3.12 recommended. Python 3.13+ works for everything except `faster-whisper` (CPU transcription); the platform automatically falls back to the OpenAI Whisper API on 3.13+.

```bash
git clone https://github.com/l0lsec/Epstein.git
cd Epstein
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

For image OCR (required for JPG/TIF scanned documents):

```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt install tesseract-ocr

# Windows
# https://github.com/UB-Mannheim/tesseract/wiki
```

### 2. Configure Environment

```bash
cp .env.example .env
# then edit .env and fill in the keys you want to use
```

At minimum, `OPENAI_API_KEY` enables the Ask AI and Summary features. Everything else has reasonable defaults.

### 3. Download Documents

```bash
# DOJ disclosures (main collection)
python scripts/download_doj_disclosures.py

# FOIA releases
python scripts/download_foia.py

# Court records
python scripts/download_court_records.py

# House Oversight Committee (Google Drive)
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID" \
    -o "./House Disclosures"
```

### 4. Extract, Index, and Run

```bash
# Extract text and build the search index, then start the server
python run.py

# Or run steps individually
python run.py extract     # OCR/extract text from all media
python run.py index       # Build/rebuild the FTS5 + vector index
python run.py server      # Start the API + UI
```

### 5. Open in Browser

[http://localhost:8000](http://localhost:8000)

The admin UI lives at [http://localhost:8000/admin.html](http://localhost:8000/admin.html) and requires `ADMIN_API_KEY` to be set.

---

## Configuration

All configuration is via environment variables (loaded automatically from `.env`).

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | _(unset)_ | Enables Ask AI, summaries, and Whisper API transcription fallback |
| `EPSTEIN_BASE_PATH` | repo root | Override the project base path (used when the server runs from a different cwd) |
| `HOST` | `0.0.0.0` | Server bind host |
| `PORT` | `8000` | Server bind port |

### Security & Access Control

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_API_KEY` | _(unset)_ | Required for `/api/admin/*` endpoints. If unset, admin endpoints are unreachable. |
| `ADMIN_IP_WHITELIST` | _(unset)_ | Optional comma-separated list of IPs additionally allowed to hit admin endpoints |
| `ALLOWED_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Comma-separated CORS allow-list |
| `ALLOWED_REFERERS` | own domain + major social platforms | Comma-separated referer allow-list for `/api/documents/{id}/file` (anti-scraping) |
| `RECAPTCHA_SECRET_KEY` | _(unset)_ | Google reCAPTCHA v3 secret. If unset, feedback spam protection is disabled. |
| `SESSION_SECRET_KEY` | random per-process | HMAC key for signed session IDs. Set explicitly in production so sessions survive restarts. |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` behind HTTPS to mark session cookies as Secure |
| `TRUSTED_PROXIES` | _(empty)_ | Comma-separated proxy IPs trusted to set `X-Forwarded-For` |
| `CLOUDFLARE_MODE` | `auto` | `auto`/`enabled`/`disabled` — controls whether `CF-Connecting-IP` is honored |

### Indexing

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTO_INDEX_ENABLED` | `false` | If `true`, the server periodically re-indexes new files in the background |
| `AUTO_INDEX_INTERVAL` | `172800` | Interval in seconds (default 48h) |

---

## Architecture

```
Epstein/
├── backend/
│   ├── server.py             FastAPI app, all HTTP endpoints, SSE maintenance stream
│   ├── database.py           SQLite FTS5 + VectorStore + index builder
│   ├── extractor.py          PDF, image OCR, and audio/video text extraction
│   ├── llm.py                OpenAI client wrapper (Ask AI, summaries)
│   └── security_logger.py    Request logging, rate limiting, session HMAC, geo lookup
├── frontend/
│   ├── index.html            Public search UI
│   ├── app.js                Public UI logic
│   ├── admin.html            Admin panel (auth-gated)
│   ├── admin.js              Admin panel logic
│   ├── maintenance.html      Maintenance status page
│   ├── styles.css
│   ├── favicon.svg
│   ├── og-image.png          OpenGraph preview image
│   └── sitemap.xml
├── scripts/
│   ├── download_doj_disclosures.py    Primary DOJ collection downloader
│   ├── download_foia.py               FOIA releases (FBI/CBP/BOP/Florida)
│   ├── download_court_records.py      Court filings
│   ├── download_gdrive.py             Google Drive folder downloader (House)
│   ├── setup_court_records.py         One-shot court-records bootstrap
│   ├── process_new_downloads.py       Re-extract + re-index for newly downloaded files
│   ├── migrate_doj_subcategories.py   Schema migration: DOJ subcategories
│   ├── migrate_hashes.py              Schema migration: file content hashes
│   ├── migrate_telemetry.py           Schema migration: telemetry tables
│   ├── cleanup_migration.py           Post-migration cleanup
│   └── scrape.js                      Headless-browser DOJ link scraper
├── run.py                    Unified CLI (setup, extract, index, server, admin tasks)
├── deploy.sh                 Optional systemd-deploy helper (parameterized via .deploy.env)
├── requirements.txt
├── .env.example
├── .deploy.env.example
├── LICENSE                   MIT
└── README.md
```

Auto-generated, not committed:
- `extracted_text/` — cached text extractions per file
- `vector_store/` — sentence-transformer embeddings and metadata
- `thumbnails/` — PDF/image preview thumbnails
- `epstein.db`, `epstein.db-wal`, `epstein.db-shm` — SQLite database
- `logs/` — security and audit logs
- `feedback.json` — user feedback inbox

---

## CLI (`run.py`)

```bash
python run.py [COMMAND] [SOURCE] [FLAGS]
```

### Commands

| Command | Description |
|---------|-------------|
| `all` _(default)_ | Run `setup` then start the server |
| `setup` | Download, extract, and index (full bootstrap) |
| `download` | Download files from configured sources only |
| `extract` | Extract text from PDFs, images, audio, and video |
| `index` | (Re)build the FTS5 + vector index |
| `server` | Start the API + UI only |
| `add PATH` | Add a single file or directory to the archive (use with `--category`) |
| `fix-fts` | Repair a corrupted FTS5 index |
| `cleanup-db` | Remove orphaned rows (no on-disk file) |
| `cleanup-duplicates` | De-duplicate documents by content hash |
| `rebuild-db` | Drop and rebuild the database from extracted text |
| `generate-thumbnails` | Regenerate PDF/image thumbnails |

### Flags

| Flag | Description |
|------|-------------|
| `--force` | Force re-extraction / re-download of all files |
| `--full-setup` | Force a complete rebuild end-to-end |
| `--reindex` | Rebuild the index before starting the server |
| `--host HOST` | Server host (default `0.0.0.0`) |
| `--port PORT` | Server port (default `8000`) |
| `--category NAME`, `-c` | Category for `add` |
| `--doj-datasets LIST` | Comma-separated DOJ dataset slugs to limit `setup`/`download` |
| `--workers N`, `-w` | Extraction worker count (default 8) |

---

## Download Scripts

### DOJ Disclosures (`download_doj_disclosures.py`)

Primary downloader for the official DOJ Epstein Document Library.

```bash
python scripts/download_doj_disclosures.py
python scripts/download_doj_disclosures.py --force        # Re-download everything
python scripts/download_doj_disclosures.py --datasets d1,d2  # Specific datasets
```

### FOIA Files (`download_foia.py`)

Downloads FOIA releases from `https://www.justice.gov/epstein/foia` organized by agency (CBP, FBI, BOP, Florida).

```bash
python scripts/download_foia.py                  # Skip existing
python scripts/download_foia.py --check-updates  # Detect re-released files
python scripts/download_foia.py --force          # Re-download all
python scripts/download_foia.py --output /path   # Custom destination
```

### Court Records (`download_court_records.py`)

Same shape as FOIA, sourced from `https://www.justice.gov/epstein/court-records`.

```bash
python scripts/download_court_records.py
python scripts/download_court_records.py --check-updates
python scripts/download_court_records.py --force
```

### Google Drive (`download_gdrive.py`)

For House Oversight Committee releases (and any other Drive folder).

```bash
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID"
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID" --list-only
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID" -o "./House Disclosures"
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID" --force
```

Idempotent. Files that fail (Drive permission errors, viruses-scan refusals, etc.) are logged to `_failed_downloads.txt`.

### Detecting Re-Releases

`--check-updates` (`-u`) compares remote file size against the local copy. If they differ, the old file is archived with a timestamped suffix (e.g., `document_20260115_143022.pdf`) and the new version is downloaded under the original name.

| Flag | Short | Description | Scripts |
|------|-------|-------------|---------|
| `--output PATH` | `-o` | Custom output directory | All |
| `--force` | `-f` | Force re-download of all files | All |
| `--check-updates` | `-u` | Check for updated files on server | FOIA, Court Records |
| `--list-only` | `-l` | List without downloading | Google Drive |

---

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats` | GET | Archive statistics |
| `/api/categories` | GET | Categories with counts |
| `/api/search` | POST | Full-text / semantic / hybrid search |
| `/api/documents` | GET | Paginated document list with filters |
| `/api/documents/{id}` | GET | Document metadata + extracted text |
| `/api/documents/{id}/file` | GET | Original file download (referer-gated) |
| `/api/documents/{id}/summary` | GET | AI-generated summary |
| `/api/documents/{id}/thumbnail` | GET | Cached preview thumbnail |
| `/api/ask` | POST | Ask a question across retrieved documents |
| `/api/feedback` | POST | Submit user feedback (reCAPTCHA-protected) |
| `/api/admin/*` | various | Admin actions (require `ADMIN_API_KEY`) |

Example:

```bash
curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "flight log", "search_type": "hybrid"}'

curl -X POST http://localhost:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Who appears in the flight logs?"}'
```

---

## Admin Panel

Set `ADMIN_API_KEY` to a long random string in `.env`, then open `/admin.html`. You'll be prompted for the key, which is stored in `localStorage` and sent as a header on every admin request.

Capabilities:

- **Documents** — search, hide/unhide, pin/unpin, reclassify file type or category, re-download, re-extract
- **Bulk actions** — multi-select documents for hide/unhide/re-extract
- **Feedback** — triage user-submitted feedback with status transitions and bulk operations
- **Telemetry** — request counts, top endpoints, error rates, geo breakdown
- **Maintenance** — toggle maintenance mode (creates/removes a `.maintenance` lock file)
- **Index control** — manual `fix-fts`, `cleanup-db`, and reindex triggers

Restrict admin access in production by setting `ADMIN_IP_WHITELIST` and/or fronting the route with nginx `allow`/`deny`.

---

## Technology Stack

- **Backend** — FastAPI, Uvicorn, Python 3.10+
- **Search** — SQLite FTS5 (lexical) + sentence-transformers (semantic)
- **PDF** — pdfplumber, PyMuPDF
- **OCR** — Tesseract via pytesseract, Pillow
- **Transcription** — Lightning Whisper MLX (Apple Silicon GPU), faster-whisper (CPU, Py ≤3.12), OpenAI Whisper API (fallback)
- **AI** — OpenAI GPT (`gpt-4-turbo` by default)
- **Downloaders** — gdown, requests, BeautifulSoup, Playwright/Puppeteer-style scrape
- **Frontend** — Vanilla HTML/CSS/JS (no build step)

---

## Production Deployment (systemd + nginx)

### 1. systemd service

```bash
sudo tee /etc/systemd/system/epstein.service << 'EOF'
[Unit]
Description=Epstein Files Search Platform
After=network.target

[Service]
User=your_username
Group=your_username
WorkingDirectory=/opt/epstein
Environment="PATH=/opt/epstein/venv/bin"
EnvironmentFile=/opt/epstein/.env
ExecStart=/opt/epstein/venv/bin/uvicorn backend.server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now epstein
```

### 2. nginx reverse proxy

```bash
sudo tee /etc/nginx/sites-available/epstein << 'EOF'
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/epstein /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Behind a proxy, also set `TRUSTED_PROXIES=127.0.0.1` and `SESSION_COOKIE_SECURE=true` in `/opt/epstein/.env`.

### 3. Optional: `deploy.sh`

`deploy.sh` is a helper that incrementally uploads changed code files via `scp`, restarts the service when backend files change, and auto-bumps cache-busting query strings in `frontend/index.html` and `frontend/admin.html`. Configure it once via a gitignored `.deploy.env`:

```bash
cp .deploy.env.example .deploy.env
# edit .deploy.env to set SSH_HOST, SSH_USER, REMOTE_DIR
./deploy.sh --dry-run     # preview what would deploy
./deploy.sh               # incremental deploy
./deploy.sh --all         # deploy every tracked code file
./deploy.sh --rollback    # restore from the on-server backup
```

---

## Performance Notes

- Initial extraction of ~45,000 documents takes 1–2 hours (PDFs are fast; image OCR dominates)
- Subsequent runs skip already-processed files (tracked by content hash)
- Image OCR throughput is ~1–2 images/second per worker
- Full-text search uses SQLite FTS5 with BM25 ranking; response cache TTLs are tuned for high read concurrency
- Semantic search uses on-disk embeddings; first query after a fresh boot is cold

---

## Contributing

Issues and pull requests welcome. There is no automated test suite yet — please describe how you tested changes in the PR body. For features that touch the schema or extraction pipeline, please run `python run.py rebuild-db` against a small subset of documents to verify the migration path.

---

## Disclaimer

This project is **not affiliated with the U.S. Department of Justice, the FBI, Congress, or any other government agency**. All documents originate from publicly released government sources, linked at the top of this README. We make no claim about the completeness, authenticity, or accuracy of the source releases or their OCR-extracted text. AI-generated summaries and answers can be wrong; treat them as starting points, not citations.

Material in this archive may include allegations, accusations, and identifying details that have not been adjudicated. Use accordingly.

## License

[MIT License](LICENSE).

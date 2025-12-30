# Epstein Library Files Public Archive

A powerful, public-facing search platform for exploring the Epstein document archive. Features full-text search, semantic AI search, and LLM-powered document analysis.

## Features

- **🔍 Full-Text Search** - Search across 45,000+ documents (PDFs, images, audio, video)
- **🧠 Semantic AI Search** - Find conceptually related documents using vector embeddings
- **💬 Ask AI** - Get intelligent answers powered by GPT-4 document analysis
- **📚 Document Browser** - Browse and explore documents by category
- **📄 PDF Viewer** - View extracted text and download original files
- **🖼️ Image OCR** - Extract text from scanned JPG/TIF documents
- **📊 AI Summaries** - Generate AI-powered document summaries

## Document Collection

| Category | Documents | Description | Source |
|----------|-----------|-------------|--------|
| DOJ Disclosures | ~14,700 | Evidence files, flight logs, contact books, reports | [justice.gov/epstein/doj-disclosures](https://www.justice.gov/epstein/doj-disclosures) |
| FOIA Files | ~100+ | FBI, CBP, BOP releases | [justice.gov/epstein/foia](https://www.justice.gov/epstein/foia) |
| Court Records | ~12,100 | Legal filings from various cases | [justice.gov/epstein/court-records](https://www.justice.gov/epstein/court-records) |
| House Disclosures | ~18,800 | DOJ-OGR scanned documents (JPG/TIF), videos (MP4), audio (WAV) | Google Drive (House Oversight Committee) |

**Total: ~45,700+ documents**

Documents are sourced from the official DOJ Epstein Document Library and congressional disclosures. Use the scripts in `scripts/` to download and update.

## Quick Start

### 1. Install Dependencies

```bash
cd /path/to/Epstein
pip install -r requirements.txt
```

**For image OCR support** (required for JPG/TIF scanned documents):
```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt install tesseract-ocr

# Windows: Download from https://github.com/UB-Mannheim/tesseract/wiki
```

### 2. Run Setup & Server

```bash
# Full setup (extract PDFs + build index + start server)
python run.py

# Or run steps separately:
python run.py setup    # Extract and index only
python run.py server   # Start server only
```

### 3. Open in Browser

Navigate to **http://localhost:8000**

## Enabling AI Features

To enable LLM-powered features (Ask AI, Document Summaries), set your OpenAI API key:

```bash
export OPENAI_API_KEY=your_openai_api_key_here
python run.py server
```

Or create a `.env` file in the project root:

```bash
# .env
OPENAI_API_KEY=your_openai_api_key_here
```

The platform will automatically load environment variables from `.env` on startup.

## Architecture

```
Epstein/
├── backend/
│   ├── server.py      # FastAPI application
│   ├── extractor.py   # PDF/media text extraction
│   ├── database.py    # SQLite FTS5 search
│   └── llm.py         # OpenAI integration
├── frontend/
│   ├── index.html     # Main UI
│   ├── styles.css     # Styling
│   └── app.js         # JavaScript app
├── scripts/           # Download & maintenance scripts
│   ├── download_foia.py           # FOIA files downloader
│   ├── download_court_records.py  # Court records downloader
│   ├── download_gdrive.py         # Google Drive folder downloader
│   └── migrate_doj_subcategories.py  # Database migration
├── extracted_text/    # Cached extractions (auto-generated)
├── vector_store/      # Semantic embeddings (auto-generated)
├── epstein.db         # SQLite database (auto-generated)
├── requirements.txt   # Python dependencies
├── run.py            # Setup & run script
└── README.md
```

## Download Scripts

The `scripts/` directory contains tools for downloading and updating the document archive from official sources.

### FOIA Files (`download_foia.py`)

Downloads FOIA files from https://www.justice.gov/epstein/foia organized by agency:
- Customs and Border Protection (CBP)
- Federal Bureau of Investigation (FBI)
- Federal Bureau of Prisons (BOP)
- Florida

```bash
# Basic download (skips existing files)
python scripts/download_foia.py

# Check for updated documents on server
python scripts/download_foia.py --check-updates

# Force re-download all files
python scripts/download_foia.py --force

# Custom output directory
python scripts/download_foia.py --output /path/to/foia
```

### Court Records (`download_court_records.py`)

Downloads court records from https://www.justice.gov/epstein/court-records organized by case:

```bash
# Basic download
python scripts/download_court_records.py

# Check for updated documents
python scripts/download_court_records.py --check-updates

# Force re-download
python scripts/download_court_records.py --force
```

### House Disclosures (`download_gdrive.py`)

Downloads files from Google Drive folders (e.g., House Oversight Committee releases):

```bash
# Download from Google Drive folder URL
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID"

# List files without downloading
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID" --list-only

# Custom output directory
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID" -o "./House Disclosures"

# Force re-download all files
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID" --force
```

Features:
- **Idempotent**: Safe to run multiple times - skips existing files
- **Resilient**: Automatically continues after permission errors on individual files
- **Folder structure preserved**: Maintains original directory hierarchy
- Files are automatically categorized as "House Disclosures" with subcategories by production folder

> **Note**: Some Google Drive files may have download restrictions. The script logs failed files to `_failed_downloads.txt`. You can download those manually through the browser UI.

### Checking for Document Updates

When documents are re-released with the same filename but different content, use the `--check-updates` (`-u`) flag:

```bash
python scripts/download_foia.py --check-updates
python scripts/download_court_records.py --check-updates
```

This will:
1. Compare the remote file size with the local file
2. If sizes differ, archive the old version with a timestamp (e.g., `document_20250115_143022.pdf`)
3. Download the new version with the original filename

### Script Options Summary

| Flag | Short | Description | Scripts |
|------|-------|-------------|---------|
| `--output PATH` | `-o` | Custom output directory | All |
| `--force` | `-f` | Force re-download of all files | All |
| `--check-updates` | `-u` | Check for updated files on server | FOIA, Court Records |
| `--list-only` | `-l` | List files without downloading | Google Drive |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats` | GET | Get archive statistics |
| `/api/categories` | GET | List document categories |
| `/api/search` | POST | Full-text/semantic search |
| `/api/documents` | GET | List documents (paginated) |
| `/api/documents/{id}` | GET | Get document details |
| `/api/documents/{id}/file` | GET | Download original PDF |
| `/api/documents/{id}/summary` | GET | AI-generated summary |
| `/api/ask` | POST | Ask a question |

### Example API Usage

```bash
# Search for documents
curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "flight log", "search_type": "hybrid"}'

# Ask a question
curl -X POST http://localhost:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Who appears in the flight logs?"}'
```

## Command Line Options

```bash
python run.py --help

# Commands:
#   setup          Download files, extract PDFs, build search index
#   download       Download files from DOJ only
#   extract        Extract text from PDFs only
#   index          Build search index only
#   server         Start the web server
#   all            Run setup then start server (default)
#   add PATH       Add new documents from a file or directory

# Flags:
#   --force        Force re-extraction of all PDFs
#   --full-setup   Force complete rebuild
#   --reindex      Rebuild index before starting server
#   --host HOST    Server host (default: 0.0.0.0)
#   --port PORT    Server port (default: 8000)
#   --category     Category for added documents (with 'add' command)
```

## Technology Stack

- **Backend**: FastAPI, Python 3.10+, Uvicorn
- **PDF Processing**: pdfplumber
- **Image OCR**: Tesseract, pytesseract, Pillow
- **Search**: SQLite FTS5 (full-text), sentence-transformers (semantic)
- **AI**: OpenAI GPT-4
- **Audio/Video**: OpenAI Whisper API (transcription), pydub
- **Downloads**: gdown (Google Drive), requests, BeautifulSoup
- **Frontend**: Vanilla HTML/CSS/JS

> **Note**: For local audio transcription with `faster-whisper`, use Python 3.12 or earlier (onnxruntime not yet available for Python 3.13+). Otherwise, transcription uses the OpenAI Whisper API.

## Keeping Documents Updated

The DOJ may release updated versions of documents or add new files. To check for updates:

```bash
# Check FOIA files for updates
python scripts/download_foia.py --check-updates

# Check court records for updates
python scripts/download_court_records.py --check-updates

# Re-download Google Drive folder (idempotent - skips existing)
python scripts/download_gdrive.py "https://drive.google.com/drive/folders/FOLDER_ID"

# After downloading new documents, re-run indexing
python run.py setup
```

When files are updated, old versions are automatically archived with timestamps (e.g., `document_20250115_143022.pdf`).

## Performance Notes

- Initial extraction of 45,000+ documents takes 1-2 hours (PDFs faster, OCR for images slower)
- Subsequent runs skip already-processed files
- Semantic search uses sentence-transformers with efficient indexing
- Full-text search uses SQLite FTS5 with BM25 ranking
- Image OCR (JPG/TIF) requires Tesseract and processes ~1-2 images/second

## Production Deployment (systemd)

For production servers, run the application as a systemd service for automatic startup and restart on failure.

### 1. Create the systemd service file

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
```

### 2. Create the environment file

```bash
sudo tee /opt/epstein/.env << 'EOF'
OPENAI_API_KEY=your_openai_key_here
RECAPTCHA_SECRET_KEY=your_recaptcha_key_here
ADMIN_API_KEY=your_admin_api_key_here
ALLOWED_ORIGINS=https://yourdomain.com
HOST=127.0.0.1
PORT=8000
EOF
sudo chmod 600 /opt/epstein/.env
```

### 3. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable epstein
sudo systemctl start epstein
```

### 4. Manage the service

```bash
# Check status
sudo systemctl status epstein

# View logs
sudo journalctl -u epstein -f

# Restart after code changes
sudo systemctl restart epstein

# Stop the service
sudo systemctl stop epstein
```

### 5. nginx reverse proxy (recommended)

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

## Disclaimer

This archive contains documents from public sources for research and transparency purposes. Some documents contain redactions. We make no claims about the completeness or accuracy of OCR-extracted text.

## License

MIT License - See LICENSE file


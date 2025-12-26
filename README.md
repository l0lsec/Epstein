# Epstein Files Search Platform

A powerful, public-facing search platform for exploring the Epstein document archive. Features full-text search, semantic AI search, and LLM-powered document analysis.

![Screenshot](https://via.placeholder.com/800x400?text=Epstein+Files+Search+Platform)

## Features

- **🔍 Full-Text Search** - Search across 16,000+ extracted PDF documents
- **🧠 Semantic AI Search** - Find conceptually related documents using vector embeddings
- **💬 Ask AI** - Get intelligent answers powered by GPT-4 document analysis
- **📚 Document Browser** - Browse and explore documents by category
- **📄 PDF Viewer** - View extracted text and download original PDFs
- **📊 AI Summaries** - Generate AI-powered document summaries

## Document Collection

| Category | Documents | Description | Source |
|----------|-----------|-------------|--------|
| DOJ Disclosures | ~14,700 | Evidence files, flight logs, contact books, reports | [justice.gov/epstein/doj-disclosures](https://www.justice.gov/epstein/doj-disclosures) |
| FOIA Files | ~100+ | FBI, CBP, BOP releases | [justice.gov/epstein/foia](https://www.justice.gov/epstein/foia) |
| Court Records | ~1,400 | Legal filings from various cases | [justice.gov/epstein/court-records](https://www.justice.gov/epstein/court-records) |

**Total: ~16,000+ documents**

Documents are sourced from the official DOJ Epstein Document Library and can be updated using the scripts in the `scripts/` directory.

## Quick Start

### 1. Install Dependencies

```bash
cd /path/to/Epstein
pip install -r requirements.txt
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
│   ├── extractor.py   # PDF text extraction
│   ├── database.py    # SQLite + ChromaDB
│   └── llm.py         # OpenAI integration
├── frontend/
│   ├── index.html     # Main UI
│   ├── styles.css     # Styling
│   └── app.js         # JavaScript app
├── scripts/           # Download & maintenance scripts
│   ├── download_foia.py           # FOIA files downloader
│   ├── download_court_records.py  # Court records downloader
│   ├── download_epstein_files.sh  # DOJ Disclosures downloader (shell)
│   └── migrate_doj_subcategories.py  # Database migration
├── extracted_text/    # Cached extractions (auto-generated)
├── vector_store/      # ChromaDB data (auto-generated)
├── epstein.db         # SQLite database (auto-generated)
├── requirements.txt   # Python dependencies
├── run.py            # Setup & run script
└── README.md
```

## Download Scripts

The `scripts/` directory contains tools for downloading and updating the document archive from official DOJ sources.

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

### DOJ Disclosures (`download_epstein_files.sh`)

Downloads EFTA files (DOJ disclosure datasets 1-8) with version tracking:

```bash
# Run the shell script
./scripts/download_epstein_files.sh
```

This script automatically:
- Compares checksums to detect changed files
- Archives old versions in a `versions/` directory
- Generates missing file reports by dataset

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

| Flag | Short | Description |
|------|-------|-------------|
| `--output PATH` | `-o` | Custom output directory |
| `--force` | `-f` | Force re-download of all files |
| `--check-updates` | `-u` | Check for updated files on server |

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

# Options:
#   setup          Extract PDFs and build search index
#   extract        Extract text from PDFs only
#   index          Build search index only
#   server         Start the web server
#   all            Run setup then start server (default)

# Flags:
#   --force        Force re-extraction of all PDFs
#   --full-setup   Force complete rebuild
#   --host HOST    Server host (default: 0.0.0.0)
#   --port PORT    Server port (default: 8000)
```

## Technology Stack

- **Backend**: FastAPI, Python 3.10+, Uvicorn
- **PDF Processing**: pdfplumber
- **Search**: SQLite FTS5 (full-text), sentence-transformers (semantic)
- **AI**: OpenAI GPT-4
- **Audio**: OpenAI Whisper (transcription), pydub
- **Frontend**: Vanilla HTML/CSS/JS

## Keeping Documents Updated

The DOJ may release updated versions of documents or add new files. To check for updates:

```bash
# Check FOIA files for updates
python scripts/download_foia.py --check-updates

# Check court records for updates
python scripts/download_court_records.py --check-updates

# After downloading new documents, re-run indexing
python run.py setup
```

When files are updated, old versions are automatically archived with timestamps (e.g., `document_20250115_143022.pdf`).

## Performance Notes

- Initial extraction of 16,000+ PDFs takes ~30-60 minutes
- Subsequent runs skip already-processed files
- Semantic search uses sentence-transformers with efficient indexing
- Full-text search uses SQLite FTS5 with BM25 ranking

## Disclaimer

This archive contains documents from public sources for research and transparency purposes. Some documents contain redactions. We make no claims about the completeness or accuracy of OCR-extracted text.

## License

MIT License - See LICENSE file


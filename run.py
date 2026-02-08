#!/usr/bin/env python3
"""
Epstein Files Search Platform - Setup and Run Script
This script handles extraction, indexing, and running the server.
"""

import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import sys
import subprocess
import shutil
import json
import hashlib
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file (skip if unreadable e.g. wrong permissions)
try:
    load_dotenv()
except PermissionError:
    pass  # .env exists but not readable (e.g. root-owned); use env vars from systemd/shell

BASE_PATH = Path(__file__).parent
BACKEND_PATH = BASE_PATH / "backend"
SCRIPTS_PATH = BASE_PATH / "scripts"
MAINTENANCE_LOCK = BASE_PATH / ".maintenance"


def enable_maintenance(message: str = "Indexing new documents..."):
    """Create maintenance lock file to trigger maintenance mode in server"""
    try:
        MAINTENANCE_LOCK.write_text(json.dumps({
            "started": datetime.now().isoformat(),
            "message": message
        }))
        print("🔧 Maintenance mode ENABLED - users will see maintenance page")
    except Exception as e:
        print(f"⚠ Could not enable maintenance mode: {e}")


def disable_maintenance():
    """Remove maintenance lock file to restore normal site operation"""
    try:
        if MAINTENANCE_LOCK.exists():
            MAINTENANCE_LOCK.unlink()
            print("✅ Maintenance mode DISABLED - site is back online")
    except Exception as e:
        print(f"⚠ Could not disable maintenance mode: {e}")


def update_maintenance_progress(step: int, step_name: str, current: int, total: int, message: str = ""):
    """Update maintenance progress for real-time display on maintenance page
    
    Args:
        step: Current step number (1-5)
        step_name: Name of current step (e.g., "Extracting PDFs")
        current: Current item being processed
        total: Total items to process
        message: Optional status message
    """
    if not MAINTENANCE_LOCK.exists():
        return
    
    try:
        # Read existing data to preserve started time
        existing = json.loads(MAINTENANCE_LOCK.read_text())
        started = existing.get("started", datetime.now().isoformat())
    except:
        started = datetime.now().isoformat()
    
    try:
        progress = {
            "started": started,
            "step": step,
            "step_name": step_name,
            "current": current,
            "total": total,
            "percent": int((current / total) * 100) if total > 0 else 0,
            "message": message
        }
        MAINTENANCE_LOCK.write_text(json.dumps(progress))
    except Exception as e:
        pass  # Don't interrupt processing for progress update failures


def check_dependencies():
    """Check if required packages are installed"""
    try:
        import fastapi
        import pdfplumber
        import sqlite_utils
        print("✓ All dependencies installed")
        return True
    except ImportError as e:
        print(f"✗ Missing dependency: {e.name}")
        print(f"\nRun: pip install -r {BASE_PATH / 'requirements.txt'}")
        return False


def setup_foia(force=False):
    """Download FOIA files from DOJ if not already present"""
    print("\n" + "="*60)
    print("STEP 0: CHECKING FOIA FILES")
    print("="*60)
    
    foia_dir = BASE_PATH / "FOIA"
    
    # Quick check - if we have category folders with files, skip
    if not force and foia_dir.exists():
        subfolders = [d for d in foia_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]
        total_files = sum(len(list(d.glob('*.*'))) for d in subfolders)
        
        if len(subfolders) >= 4 and total_files >= 100:
            print(f"✓ FOIA files present ({len(subfolders)} categories, {total_files} files)")
            
            print("\n  Categories:")
            for folder in sorted(subfolders):
                count = len(list(folder.glob('*.*')))
                print(f"    {count:4d}  {folder.name}")
            
            return True
    
    # Import and run the download script
    sys.path.insert(0, str(SCRIPTS_PATH))
    try:
        from download_foia import setup_foia as download_foia_files
        result = download_foia_files(output_dir=foia_dir, force=force, verbose=True)
        
        if result['downloaded'] > 0:
            print(f"\n✓ Downloaded {result['downloaded']} new files")
        
        return result['failed'] == 0
    except ImportError as e:
        print(f"⚠ Could not import FOIA download script: {e}")
        print("  Run manually: python scripts/download_foia.py")
        return False
    except Exception as e:
        print(f"⚠ Error downloading FOIA files: {e}")
        print("  You can run manually: python scripts/download_foia.py")
        return False


def setup_court_records(force=False):
    """Download court records from DOJ if not already present"""
    print("\n" + "="*60)
    print("STEP 1: CHECKING COURT RECORDS")
    print("="*60)
    
    court_records_dir = BASE_PATH / "CourtRecords"
    
    # Quick check - if we have enough files, skip
    if not force and court_records_dir.exists():
        pdf_count = len(list(court_records_dir.glob("**/*.pdf")))
        case_count = len([d for d in court_records_dir.iterdir() if d.is_dir() and not d.name.startswith('.')])
        
        if pdf_count >= 1400 and case_count >= 30:
            print(f"✓ Court records present ({case_count} cases, {pdf_count} files)")
            
            # Show top cases
            print("\n  Cases by file count (top 10):")
            case_sizes = []
            for case_dir in court_records_dir.iterdir():
                if case_dir.is_dir() and not case_dir.name.startswith('.'):
                    count = len(list(case_dir.glob("*.pdf")))
                    case_sizes.append((case_dir.name, count))
            
            for name, count in sorted(case_sizes, key=lambda x: -x[1])[:10]:
                print(f"    {count:4d}  {name[:50]}")
            
            return True
    
    # Import and run the setup script
    sys.path.insert(0, str(SCRIPTS_PATH))
    try:
        from setup_court_records import setup_court_records as download_court_records
        result = download_court_records(output_dir=court_records_dir, force=force, verbose=True)
        
        if result['downloaded'] > 0:
            print(f"\n✓ Downloaded {result['downloaded']} new files")
        
        return result['failed'] == 0
    except ImportError as e:
        print(f"⚠ Could not import court records setup script: {e}")
        print("  Run manually: python scripts/setup_court_records.py")
        return False
    except Exception as e:
        print(f"⚠ Error downloading court records: {e}")
        print("  You can run manually: python scripts/setup_court_records.py")
        return False


def setup_doj_disclosures(force=False, datasets=None):
    """Download DOJ Disclosures data sets if not already present
    
    Args:
        force: Force re-download of all files
        datasets: List of specific data set numbers to download (default: auto-detect existing folders)
    """
    print("\n" + "="*60)
    print("STEP 1b: CHECKING DOJ DISCLOSURES")
    print("="*60)
    
    doj_dir = BASE_PATH / "DOJ Disclosures"
    
    # Dynamically detect existing Data Set folders if not specified
    if datasets is None:
        if doj_dir.exists():
            import re
            detected = []
            for folder in doj_dir.iterdir():
                if folder.is_dir():
                    match = re.match(r'^Data Set (\d+)$', folder.name)
                    if match:
                        detected.append(int(match.group(1)))
            datasets = sorted(detected) if detected else [9, 10, 11, 12]  # Fallback to new sets
        else:
            datasets = [9, 10, 11, 12]  # Default for fresh install
    
    # Quick check - if we have the data set folders with files, skip
    if not force and doj_dir.exists():
        existing_datasets = []
        total_files = 0
        
        for ds_num in datasets:
            ds_dir = doj_dir / f"Data Set {ds_num}"
            if ds_dir.exists():
                count = len(list(ds_dir.glob("*.pdf")))
                if count > 0:
                    existing_datasets.append((ds_num, count))
                    total_files += count
        
        if existing_datasets and total_files > 100:
            print(f"✓ DOJ Disclosures present ({len(existing_datasets)} data sets, {total_files} files)")
            
            print("\n  Data Sets:")
            for ds_num, count in existing_datasets:
                print(f"    {count:5d}  Data Set {ds_num}")
            
            return True
    
    # Import and run the download script
    sys.path.insert(0, str(SCRIPTS_PATH))
    try:
        from download_doj_disclosures import setup_doj_disclosures as download_doj_files
        result = download_doj_files(
            output_dir=doj_dir, 
            datasets=datasets,
            force=force, 
            verbose=True
        )
        
        if result['downloaded'] > 0:
            print(f"\n✓ Downloaded {result['downloaded']} new files")
        
        return result['failed'] == 0
    except ImportError as e:
        print(f"⚠ Could not import DOJ Disclosures download script: {e}")
        print("  Run manually: python scripts/download_doj_disclosures.py")
        return False
    except Exception as e:
        print(f"⚠ Error downloading DOJ Disclosures: {e}")
        print("  You can run manually: python scripts/download_doj_disclosures.py")
        return False


def cleanup_stale_documents():
    """Remove database entries for files that no longer exist"""
    import sqlite3
    
    db_path = BASE_PATH / "epstein.db"
    if not db_path.exists():
        return 0
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all documents
    cursor.execute("SELECT id, path, category FROM documents")
    documents = cursor.fetchall()
    
    missing_ids = []
    categories_affected = {}
    
    for doc in documents:
        file_path = BASE_PATH / doc['path']
        if not file_path.exists():
            missing_ids.append(doc['id'])
            cat = doc['category'] or 'Unknown'
            categories_affected[cat] = categories_affected.get(cat, 0) + 1
    
    if missing_ids:
        # Delete from FTS first
        for doc_id in missing_ids:
            cursor.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
        # Delete from main table
        cursor.executemany("DELETE FROM documents WHERE id = ?", [(id,) for id in missing_ids])
        conn.commit()
        print(f"  Cleaned up {len(missing_ids)} stale database entries:")
        for cat, count in sorted(categories_affected.items(), key=lambda x: -x[1]):
            print(f"    - {cat}: {count} removed")
    
    conn.close()
    return len(missing_ids)


def show_extraction_status():
    """Show current extraction status by category"""
    import sqlite3
    
    db_path = BASE_PATH / "epstein.db"
    if not db_path.exists():
        print("  No database yet - will be created during extraction")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Count by category
    cursor.execute("""
        SELECT category, COUNT(*) as count 
        FROM documents 
        GROUP BY category 
        ORDER BY count DESC
    """)
    categories = cursor.fetchall()
    
    print("\n  Current database status:")
    total = 0
    for cat, count in categories:
        print(f"    {count:5d}  {cat}")
        total += count
    print(f"    {'─'*20}")
    print(f"    {total:5d}  Total")
    
    # Count actual files
    pdf_count = len(list(BASE_PATH.glob("**/*.pdf")))
    print(f"\n  PDF files on disk: {pdf_count}")
    
    if pdf_count > total:
        print(f"  ⚠ {pdf_count - total} files need extraction")
    
    conn.close()


def extract_all_media(force=False, max_workers=8):
    """Extract text from PDFs and transcribe audio/video files
    
    Args:
        force: Force re-extraction of all files
        max_workers: Number of parallel workers (default: 8)
    """
    print("\n" + "="*60)
    print("STEP 2: EXTRACTING TEXT & TRANSCRIBING MEDIA")
    print("="*60)
    print(f"  Using {max_workers} parallel workers")
    
    # Show current status
    show_extraction_status()
    
    # Clean up stale entries first
    print("\n  Checking for stale entries...")
    cleanup_stale_documents()
    
    sys.path.insert(0, str(BACKEND_PATH))
    from extractor import MediaExtractor
    
    print("\n  Starting extraction (this may take a while)...")
    extractor = MediaExtractor(str(BASE_PATH))
    
    # Create progress callbacks for each extraction type
    def pdf_progress(current, total):
        update_maintenance_progress(1, "Extracting PDFs", current, total, f"Processing PDF {current} of {total}")
    
    def image_progress(current, total):
        update_maintenance_progress(2, "Extracting Images (OCR)", current, total, f"Processing image {current} of {total}")
    
    def media_progress(current, total):
        update_maintenance_progress(3, "Transcribing Media", current, total, f"Transcribing file {current} of {total}")
    
    results = extractor.extract_all(
        max_workers=max_workers, 
        force=force,
        pdf_progress_callback=pdf_progress,
        image_progress_callback=image_progress,
        media_progress_callback=media_progress
    )
    
    print(f"\n✓ Extraction complete:")
    print(f"  📄 PDFs   - Success: {results['pdf']['success']}, Failed: {results['pdf']['failed']}, Skipped: {results['pdf']['skipped']}")
    print(f"  🖼️  Images - Success: {results['image']['success']}, Failed: {results['image']['failed']}, Skipped: {results['image']['skipped']}")
    print(f"  🎤 Media  - Success: {results['media']['success']}, Failed: {results['media']['failed']}, Skipped: {results['media']['skipped']}")
    
    # Show final status
    show_extraction_status()
    
    return results['total_success'] > 0 or results['total_skipped'] > 0


def extract_pdfs(force=False):
    """Extract text from all PDF files only (legacy function)"""
    print("\n" + "="*60)
    print("STEP 2: EXTRACTING TEXT FROM PDFs")
    print("="*60)
    
    # Show current status
    show_extraction_status()
    
    # Clean up stale entries first
    print("\n  Checking for stale entries...")
    cleanup_stale_documents()
    
    sys.path.insert(0, str(BACKEND_PATH))
    from extractor import PDFExtractor
    
    print("\n  Starting extraction (this may take a while)...")
    extractor = PDFExtractor(str(BASE_PATH))
    results = extractor.extract_all(max_workers=8, force=force)
    
    print(f"\n✓ Extraction complete:")
    print(f"  - Success: {results['success']}")
    print(f"  - Failed: {results['failed']}")
    print(f"  - Skipped (already processed): {results['skipped']}")
    
    # Show final status
    show_extraction_status()
    
    return results['success'] > 0 or results['skipped'] > 0


def build_index(force: bool = False):
    """Build search database and vector store
    
    Args:
        force: If True, re-index all documents. If False, only index new documents.
    """
    print("\n" + "="*60)
    print("STEP 3: BUILDING SEARCH INDEX")
    print("="*60)
    
    sys.path.insert(0, str(BACKEND_PATH))
    from database import build_index as db_build_index
    
    # Create progress callbacks for indexing steps
    def index_progress(current, total):
        update_maintenance_progress(4, "Building Search Index", current, total, f"Indexing document {current} of {total}")
    
    def embedding_progress(current, total):
        update_maintenance_progress(5, "Generating AI Embeddings", current, total, f"Embedding {current} of {total}")
    
    db_build_index(
        str(BASE_PATH), 
        force=force,
        index_progress_callback=index_progress,
        embedding_progress_callback=embedding_progress
    )
    print("✓ Index built successfully")


def generate_all_thumbnails(max_workers=4):
    """Pre-generate thumbnails for all documents to reduce disk I/O during requests
    
    This significantly improves performance by eliminating PDF reads during thumbnail requests.
    """
    print("\n" + "="*60)
    print("GENERATING THUMBNAILS")
    print("="*60)
    
    import sqlite3
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from tqdm import tqdm
    
    # Thumbnail settings (must match server.py)
    THUMBNAIL_WIDTH = 200
    THUMBNAIL_HEIGHT = 280
    THUMBNAILS_PATH = BASE_PATH / "thumbnails"
    THUMBNAILS_PATH.mkdir(exist_ok=True)
    
    db_path = BASE_PATH / "epstein.db"
    if not db_path.exists():
        print("ERROR: Database not found. Run 'python run.py index' first.")
        return
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # Get all documents
    cursor.execute("SELECT id, path, file_type FROM documents")
    documents = cursor.fetchall()
    conn.close()
    
    print(f"Found {len(documents):,} documents")
    
    # Filter documents that need thumbnails
    to_generate = []
    already_exists = 0
    
    for doc_id, doc_path, file_type in documents:
        thumbnail_path = THUMBNAILS_PATH / f"{doc_id}.jpg"
        if thumbnail_path.exists():
            already_exists += 1
        else:
            to_generate.append((doc_id, doc_path, file_type or "pdf"))
    
    print(f"  {already_exists:,} thumbnails already exist")
    print(f"  {len(to_generate):,} thumbnails to generate")
    
    if not to_generate:
        print("\n✓ All thumbnails already generated!")
        return
    
    # Import thumbnail generation functions
    def generate_pdf_thumbnail(pdf_path: Path, output_path: Path) -> bool:
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))
            if doc.page_count == 0:
                doc.close()
                return False
            page = doc[0]
            page_rect = page.rect
            zoom_x = THUMBNAIL_WIDTH / page_rect.width
            zoom_y = THUMBNAIL_HEIGHT / page_rect.height
            zoom = min(zoom_x, zoom_y)
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(str(output_path))
            doc.close()
            return True
        except:
            return False
    
    def generate_image_thumbnail(image_path: Path, output_path: Path) -> bool:
        try:
            from PIL import Image
            with Image.open(str(image_path)) as img:
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGB')
                img.thumbnail((THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), Image.Resampling.LANCZOS)
                img.save(str(output_path), 'JPEG', quality=85)
            return True
        except:
            return False
    
    def create_placeholder(file_type: str, output_path: Path) -> bool:
        try:
            from PIL import Image, ImageDraw
            colors = {'audio': '#4a5568', 'video': '#2d3748', 'document': '#2a2a40'}
            img = Image.new('RGB', (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), color=colors.get(file_type, '#2a2a40'))
            draw = ImageDraw.Draw(img)
            icons = {'audio': '🎵', 'video': '🎬', 'document': '📄'}
            icon = icons.get(file_type, '📄')
            draw.text((THUMBNAIL_WIDTH//2 - 20, THUMBNAIL_HEIGHT//2 - 30), icon, fill='white')
            img.save(str(output_path), 'JPEG', quality=85)
            return True
        except:
            return False
    
    def process_thumbnail(args):
        doc_id, doc_path, file_type = args
        thumbnail_path = THUMBNAILS_PATH / f"{doc_id}.jpg"
        file_path = BASE_PATH / doc_path
        
        if not file_path.exists():
            create_placeholder("document", thumbnail_path)
            return "placeholder"
        
        if file_type == "pdf":
            if generate_pdf_thumbnail(file_path, thumbnail_path):
                return "success"
            else:
                create_placeholder("document", thumbnail_path)
                return "placeholder"
        elif file_type == "image":
            if generate_image_thumbnail(file_path, thumbnail_path):
                return "success"
            else:
                create_placeholder("image", thumbnail_path)
                return "placeholder"
        elif file_type in ("audio", "video"):
            create_placeholder(file_type, thumbnail_path)
            return "placeholder"
        else:
            create_placeholder("document", thumbnail_path)
            return "placeholder"
    
    # Process thumbnails in parallel
    print(f"\nGenerating thumbnails with {max_workers} workers...")
    results = {"success": 0, "placeholder": 0, "failed": 0}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_thumbnail, args): args for args in to_generate}
        
        for future in tqdm(as_completed(futures), total=len(to_generate), desc="Generating"):
            try:
                result = future.result()
                results[result] = results.get(result, 0) + 1
            except Exception as e:
                results["failed"] += 1
    
    print(f"\n✓ Thumbnail generation complete:")
    print(f"  Success: {results['success']:,}")
    print(f"  Placeholders: {results['placeholder']:,}")
    print(f"  Failed: {results['failed']:,}")


def run_server(host="0.0.0.0", port=8000):
    """Start the FastAPI server"""
    print("\n" + "="*60)
    print("STEP 4: STARTING SERVER")
    print("="*60)
    
    os.chdir(BACKEND_PATH)
    os.environ["EPSTEIN_BASE_PATH"] = str(BASE_PATH)
    
    print(f"\n🚀 Starting server at http://{host}:{port}")
    print(f"   API Docs: http://{host}:{port}/docs")
    print(f"\nPress Ctrl+C to stop the server\n")
    
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "server:app",
        "--host", host,
        "--port", str(port),
        "--reload"
    ])


def add_documents(source_path, category=None):
    """Add new documents (PDFs, audio, video) to the archive"""
    source = Path(source_path)
    
    if not source.exists():
        print(f"✗ Error: Path does not exist: {source}")
        return False
    
    # Supported extensions
    SUPPORTED_EXTENSIONS = {
        '.pdf',  # Documents
        '.wav', '.mp3', '.m4a', '.ogg', '.flac', '.aac', '.wma',  # Audio
        '.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv'   # Video
    }
    
    # Determine category/destination
    if category:
        dest_folder = BASE_PATH / category
    else:
        dest_folder = BASE_PATH / "NewDocuments"
    
    dest_folder.mkdir(exist_ok=True)
    
    # Collect all supported files
    files_to_add = []
    if source.is_file():
        if source.suffix.lower() in SUPPORTED_EXTENSIONS:
            files_to_add = [source]
        else:
            print(f"✗ Error: Unsupported file type: {source.suffix}")
            print(f"  Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
            return False
    else:
        for ext in SUPPORTED_EXTENSIONS:
            files_to_add.extend(source.rglob(f"*{ext}"))
            files_to_add.extend(source.rglob(f"*{ext.upper()}"))
    
    if not files_to_add:
        print(f"✗ No supported files found in: {source}")
        print(f"  Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return False
    
    # Count by type
    pdfs = [f for f in files_to_add if f.suffix.lower() == '.pdf']
    audio = [f for f in files_to_add if f.suffix.lower() in {'.wav', '.mp3', '.m4a', '.ogg', '.flac', '.aac', '.wma'}]
    video = [f for f in files_to_add if f.suffix.lower() in {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv'}]
    
    print(f"\n📁 Found {len(files_to_add)} file(s):")
    if pdfs: print(f"   📄 {len(pdfs)} PDF documents")
    if audio: print(f"   🎵 {len(audio)} audio files")
    if video: print(f"   🎬 {len(video)} video files")
    print(f"📂 Destination: {dest_folder}")
    
    # Copy files
    copied = 0
    skipped = 0
    for file in files_to_add:
        dest_file = dest_folder / file.name
        
        # Handle duplicates
        if dest_file.exists():
            base = dest_file.stem
            suffix = dest_file.suffix
            counter = 1
            while dest_file.exists():
                dest_file = dest_folder / f"{base}_{counter}{suffix}"
                counter += 1
        
        try:
            shutil.copy2(file, dest_file)
            copied += 1
            icon = "📄" if file.suffix.lower() == '.pdf' else ("🎵" if file.suffix.lower() in {'.wav', '.mp3', '.m4a', '.ogg', '.flac', '.aac', '.wma'} else "🎬")
            print(f"  {icon} Copied: {file.name}")
        except Exception as e:
            print(f"  ✗ Failed to copy {file.name}: {e}")
            skipped += 1
    
    print(f"\n✓ Copied {copied} files, skipped {skipped}")
    
    if copied > 0:
        print("\n🔄 Processing and indexing new files...")
        # Extract/transcribe the new files
        extract_all_media(force=False)
        build_index()
        print("\n✅ New files added and indexed!")
        print(f"   They are now searchable in the '{category or 'NewDocuments'}' category")
    
    return copied > 0


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Epstein Files Search Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py setup                    # Download all sources, extract PDFs, build index
  python run.py download                 # Only download files from DOJ (no extraction)
  python run.py server                   # Start the web server
  python run.py server --reindex         # Rebuild index then start server
  python run.py --full-setup             # Force re-download and rebuild everything
  python run.py                          # Run setup (if needed) then start server
  
DOJ Disclosures (Data Sets 1-12, auto-detects existing folders):
  python run.py download                             # Download all sources (auto-detects data sets)
  python run.py download --doj-datasets 9,10,11,12   # Download only specific data sets
  python scripts/download_doj_disclosures.py -d 9    # Download Data Set 9 only (standalone)
  
Adding new files (PDFs, audio, video):
  python run.py add /path/to/file.pdf                    # Add single PDF
  python run.py add /path/to/recording.mp4               # Add single video (transcribed)
  python run.py add /path/to/folder/                     # Add all supported files
  python run.py add /path/to/files --category "Evidence" # Add to specific category

Database maintenance:
  python run.py fix-fts                                  # Rebuild FTS5 full-text search index
  python run.py cleanup-db                               # Remove orphaned/duplicate entries
  python run.py cleanup-duplicates                       # Remove duplicate documents (same path)
  python run.py rebuild-db                                # Replace corrupted DB/vector store and re-index

Supported formats:
  Documents: .pdf
  Audio: .wav, .mp3, .m4a, .ogg, .flac, .aac, .wma
  Video: .mp4, .mov, .avi, .mkv, .webm, .m4v, .wmv
  
Data Sources:
  Court Records: https://www.justice.gov/epstein/court-records
  DOJ Disclosures: https://www.justice.gov/epstein/doj-disclosures (Data Sets 1-12)
  FOIA: https://www.justice.gov/epstein/foia
        """
    )
    
    parser.add_argument("command", nargs="?", default="all",
                        choices=["setup", "extract", "index", "server", "all", "add", "download", "fix-fts", "cleanup-db", "cleanup-duplicates", "rebuild-db", "generate-thumbnails"],
                        help="Command to run")
    parser.add_argument("source", nargs="?", default=None,
                        help="Source path for 'add' command (file or directory)")
    parser.add_argument("--category", "-c", default=None,
                        help="Category folder for new documents (default: NewDocuments)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-extraction of all PDFs")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Server host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000,
                        help="Server port (default: 8000)")
    parser.add_argument("--full-setup", action="store_true",
                        help="Force complete setup (extract + index)")
    parser.add_argument("--reindex", action="store_true",
                        help="Rebuild the search index before starting server")
    parser.add_argument("--doj-datasets", type=str, default=None,
                        help="Comma-separated DOJ data sets to download (e.g., '9,10,11,12'). Default: 9,10,11,12")
    parser.add_argument("--workers", "-w", type=int, default=8,
                        help="Number of parallel workers for extraction (default: 8, max recommended: 16)")
    
    args = parser.parse_args()
    
    print("""
╔═══════════════════════════════════════════════════════════════╗
║         EPSTEIN LIBRARY FILES PUBLIC ARCHIVE                 ║
║         Public Document Archive & AI Analysis                 ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Check for OpenAI API key
    if os.getenv("OPENAI_API_KEY"):
        print("✓ OpenAI API key configured (LLM features enabled)")
    else:
        print("⚠ OPENAI_API_KEY not set (LLM features will be disabled)")
        print("  To enable AI features, set: export OPENAI_API_KEY=your_key")
    
    force = args.force or args.full_setup
    
    # Handle add command
    if args.command == "add":
        if not args.source:
            print("✗ Error: Please provide a source path")
            print("  Usage: python run.py add /path/to/pdfs [--category NAME]")
            sys.exit(1)
        add_documents(args.source, category=args.category)
        sys.exit(0)
    
    # Handle fix-fts command
    if args.command == "fix-fts":
        print("\n" + "="*60)
        print("REBUILDING FTS5 FULL-TEXT SEARCH INDEX")
        print("="*60)
        
        from backend.database import Database
        db_path = BASE_PATH / "epstein.db"
        if not db_path.exists():
            print("✗ Database not found. Run 'python run.py setup' first.")
            sys.exit(1)
        
        def _fts_progress(phase, current, total, message):
            if phase == 3 and total > 0:
                pct = int(100 * current / total)
                print(f"\r  {message} {current:,}/{total:,} ({pct}%)  ", end="", flush=True)
            else:
                print(f"\n  {message}")
        
        db = Database(str(db_path))
        db.rebuild_fts(progress_callback=_fts_progress)
        print("\n✅ FTS5 index rebuilt successfully!")
        sys.exit(0)
    
    # Handle cleanup-db command
    if args.command == "cleanup-db":
        print("\n" + "="*60)
        print("DATABASE CLEANUP - Removing orphaned entries & artifacts")
        print("="*60)
        
        import sqlite3
        db_path = BASE_PATH / "epstein.db"
        if not db_path.exists():
            print("✗ Database not found. Run 'python run.py setup' first.")
            sys.exit(1)
        
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Define artifact paths
        THUMBNAILS_PATH = BASE_PATH / "thumbnails"
        EXTRACTED_PATH = BASE_PATH / "extracted_text"
        VECTOR_STORE_PATH = BASE_PATH / "vector_store"
        
        # Find documents with non-existent files
        print("\n🔍 Checking for orphaned database entries...")
        cursor.execute("SELECT id, path, filename FROM documents")
        orphaned = []
        for row in cursor.fetchall():
            file_path = BASE_PATH / row['path']
            if not file_path.exists():
                orphaned.append((row['id'], row['path'], row['filename']))
        
        if orphaned:
            print(f"  Found {len(orphaned)} orphaned entries:")
            for doc_id, path, filename in orphaned[:10]:
                print(f"    - {filename} ({path})")
            if len(orphaned) > 10:
                print(f"    ... and {len(orphaned) - 10} more")
            
            confirm = input("\n  Delete these entries and associated artifacts? [y/N]: ")
            if confirm.lower() == 'y':
                deleted_thumbnails = 0
                deleted_extracted = 0
                deleted_summaries = 0
                deleted_pinned = 0
                
                for doc_id, path, filename in orphaned:
                    # Delete from documents table
                    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                    
                    # Delete from summaries table
                    cursor.execute("DELETE FROM summaries WHERE document_id = ?", (doc_id,))
                    if cursor.rowcount > 0:
                        deleted_summaries += 1
                    
                    # Delete from pinned_documents table
                    cursor.execute("DELETE FROM pinned_documents WHERE document_id = ?", (doc_id,))
                    if cursor.rowcount > 0:
                        deleted_pinned += 1
                    
                    # Delete thumbnail file
                    thumbnail_path = THUMBNAILS_PATH / f"{doc_id}.jpg"
                    if thumbnail_path.exists():
                        try:
                            thumbnail_path.unlink()
                            deleted_thumbnails += 1
                        except Exception as e:
                            print(f"    ⚠ Could not delete thumbnail {doc_id}.jpg: {e}")
                    
                    # Delete extracted text JSON file
                    extracted_json = EXTRACTED_PATH / f"{doc_id}.json"
                    if extracted_json.exists():
                        try:
                            extracted_json.unlink()
                            deleted_extracted += 1
                        except Exception as e:
                            print(f"    ⚠ Could not delete extracted text {doc_id}.json: {e}")
                
                conn.commit()
                print(f"\n  ✓ Deleted {len(orphaned)} orphaned database entries")
                print(f"  ✓ Deleted {deleted_thumbnails} thumbnail files")
                print(f"  ✓ Deleted {deleted_extracted} extracted text files")
                if deleted_summaries > 0:
                    print(f"  ✓ Deleted {deleted_summaries} cached summaries")
                if deleted_pinned > 0:
                    print(f"  ✓ Deleted {deleted_pinned} pinned document entries")
                
                # Update extracted text index files
                print("\n  🔄 Updating extracted text indexes...")
                orphaned_ids = {doc_id for doc_id, _, _ in orphaned}
                for index_name in ["index.json", "image_index.json", "media_index.json"]:
                    index_file = EXTRACTED_PATH / index_name
                    if index_file.exists():
                        try:
                            with open(index_file, 'r') as f:
                                index_data = json.load(f)
                            
                            original_count = len(index_data.get("files", {}))
                            index_data["files"] = {
                                k: v for k, v in index_data.get("files", {}).items()
                                if k not in orphaned_ids
                            }
                            new_count = len(index_data.get("files", {}))
                            
                            if new_count < original_count:
                                with open(index_file, 'w') as f:
                                    json.dump(index_data, f, indent=2)
                                print(f"    - Updated {index_name}: removed {original_count - new_count} entries")
                        except Exception as e:
                            print(f"    ⚠ Could not update {index_name}: {e}")
                
                # Rebuild FTS index
                print("\n  🔄 Rebuilding FTS index...")
                from backend.database import Database
                db = Database(str(db_path))
                db.rebuild_fts()
                print("  ✓ FTS index rebuilt")
                
                # Note about vector store
                print("\n  ⚠ Note: Vector store may contain stale embeddings.")
                print("    Run 'python run.py index --force' to rebuild vector embeddings.")
            else:
                print("  ⚠ Skipped deletion")
        else:
            print("  ✓ No orphaned database entries found")
        
        # Check for orphaned artifacts (artifacts without database entries)
        print("\n🔍 Checking for orphaned artifacts (files without database entries)...")
        
        # Get all document IDs in database
        cursor.execute("SELECT id FROM documents")
        valid_ids = {row['id'] for row in cursor.fetchall()}
        
        orphaned_thumbnails = []
        orphaned_extracted = []
        
        # Check thumbnails directory
        if THUMBNAILS_PATH.exists():
            for thumb_file in THUMBNAILS_PATH.glob("*.jpg"):
                doc_id = thumb_file.stem
                if doc_id not in valid_ids:
                    orphaned_thumbnails.append(thumb_file)
        
        # Check extracted text directory
        if EXTRACTED_PATH.exists():
            for json_file in EXTRACTED_PATH.glob("*.json"):
                if json_file.name in ["index.json", "image_index.json", "media_index.json"]:
                    continue  # Skip index files
                doc_id = json_file.stem
                if doc_id not in valid_ids:
                    orphaned_extracted.append(json_file)
        
        if orphaned_thumbnails or orphaned_extracted:
            print(f"  Found {len(orphaned_thumbnails)} orphaned thumbnails")
            print(f"  Found {len(orphaned_extracted)} orphaned extracted text files")
            
            if orphaned_thumbnails:
                print("\n  Orphaned thumbnails (first 10):")
                for f in orphaned_thumbnails[:10]:
                    print(f"    - {f.name}")
                if len(orphaned_thumbnails) > 10:
                    print(f"    ... and {len(orphaned_thumbnails) - 10} more")
            
            confirm = input("\n  Delete these orphaned artifact files? [y/N]: ")
            if confirm.lower() == 'y':
                deleted_count = 0
                for f in orphaned_thumbnails + orphaned_extracted:
                    try:
                        f.unlink()
                        deleted_count += 1
                    except Exception as e:
                        print(f"    ⚠ Could not delete {f.name}: {e}")
                print(f"  ✓ Deleted {deleted_count} orphaned artifact files")
            else:
                print("  ⚠ Skipped artifact deletion")
        else:
            print("  ✓ No orphaned artifacts found")
        
        # Find and report duplicates
        print("\n🔍 Checking for duplicate entries (same filename)...")
        cursor.execute("""
            SELECT filename, COUNT(*) as cnt, GROUP_CONCAT(id, ', ') as ids
            FROM documents 
            GROUP BY filename 
            HAVING cnt > 1
            ORDER BY cnt DESC
            LIMIT 20
        """)
        duplicates = cursor.fetchall()
        
        if duplicates:
            print(f"  Found {len(duplicates)} filenames with multiple entries:")
            for row in duplicates[:10]:
                print(f"    - {row['filename']} ({row['cnt']} entries)")
        else:
            print("  ✓ No duplicates found")
        
        conn.close()
        print("\n✅ Database cleanup complete!")
        sys.exit(0)
    
    # Handle cleanup-duplicates command (remove duplicate rows with same path)
    if args.command == "cleanup-duplicates":
        import sqlite3
        print("\n" + "="*60)
        print("CLEANUP DUPLICATES - One row per path")
        print("="*60)
        
        db_path = BASE_PATH / "epstein.db"
        if not db_path.exists():
            print("✗ Database not found. Run 'python run.py setup' first.")
            sys.exit(1)
        
        def _canonical_doc_hash(path: str, base: Path) -> str:
            try:
                full = base / path
                if full.exists():
                    size = full.stat().st_size
                    return hashlib.md5(f"{path}_{size}".encode()).hexdigest()
            except Exception:
                pass
            return ""
        
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, path FROM documents")
        rows = cursor.fetchall()
        conn.close()
        
        # Group by path
        from collections import defaultdict
        path_to_ids = defaultdict(list)
        for row in rows:
            path_to_ids[row["path"]].append(row["id"])
        
        # For each path with duplicates, choose one to keep (prefer canonical hash id)
        ids_to_remove = []
        for path, ids in path_to_ids.items():
            if len(ids) <= 1:
                continue
            canonical = _canonical_doc_hash(path, BASE_PATH)
            if canonical and canonical in ids:
                keep_id = canonical
            else:
                keep_id = ids[0]
            for doc_id in ids:
                if doc_id != keep_id:
                    ids_to_remove.append(doc_id)
        
        if not ids_to_remove:
            print("✓ No duplicate paths found. Database already has one row per path.")
            sys.exit(0)
        
        print(f"  Found {len(ids_to_remove)} duplicate row(s) to remove (keeping one per path).")
        confirm = input("  Proceed? [y/N]: ")
        if confirm.lower() != 'y':
            print("  Skipped.")
            sys.exit(0)
        
        # Check database integrity before making changes
        conn = sqlite3.connect(str(db_path))
        try:
            r = conn.execute("PRAGMA integrity_check").fetchone()
            if r[0] != "ok":
                print(f"  ✗ Database integrity check failed: {r[0]}")
                print("  Fix or restore epstein.db (e.g. from backup, or try sqlite3 .recover) then run again.")
                conn.close()
                sys.exit(1)
        finally:
            conn.close()
        
        sys.path.insert(0, str(BACKEND_PATH))
        from database import Database, VectorStore
        
        db = Database(str(db_path))
        vector_store = VectorStore(str(BASE_PATH / "vector_store"))
        
        # Delete in batches to avoid one huge transaction (reduces risk of corruption)
        batch_size = 5000
        ids_actually_removed = []
        failed_batches = 0
        with db.get_connection() as conn:
            for i in range(0, len(ids_to_remove), batch_size):
                batch = ids_to_remove[i : i + batch_size]
                placeholders = ",".join("?" * len(batch))
                try:
                    conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", batch)
                    conn.execute(f"DELETE FROM summaries WHERE document_id IN ({placeholders})", batch)
                    conn.execute(f"DELETE FROM pinned_documents WHERE document_id IN ({placeholders})", batch)
                    conn.commit()
                    ids_actually_removed.extend(batch)
                except sqlite3.DatabaseError as e:
                    failed_batches += 1
                    conn.rollback()
                    print(f"  ⚠ Batch {i // batch_size + 1} failed ({e}); skipping.")
        
        removed_vectors = vector_store.remove_doc_ids(set(ids_actually_removed))
        print(f"  ✓ Removed {len(ids_actually_removed)} duplicate document row(s) from database")
        if failed_batches:
            print(f"  ⚠ {failed_batches} batch(es) skipped due to database errors (run PRAGMA integrity_check; recover or restore epstein.db then run again)")
        print(f"  ✓ Removed {removed_vectors} embedding(s) from vector store")
        print("\n✅ Cleanup complete!")
        sys.exit(0)
    
    # Handle rebuild-db command (remove corrupted DB and optionally vector store; then re-index)
    if args.command == "rebuild-db":
        print("\n" + "="*60)
        print("REBUILD DATABASE - Fresh DB and re-index from extracted_text")
        print("="*60)
        
        extracted_dir = BASE_PATH / "extracted_text"
        if not extracted_dir.exists():
            print("✗ extracted_text/ not found. Run extract first (e.g. python run.py extract).")
            sys.exit(1)
        index_file = extracted_dir / "index.json"
        if not index_file.exists():
            print("✗ extracted_text/index.json not found. Run extract first.")
            sys.exit(1)
        
        db_path = BASE_PATH / "epstein.db"
        vector_dir = BASE_PATH / "vector_store"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if db_path.exists():
            backup_db = BASE_PATH / f"epstein.db.corrupt_{ts}"
            db_path.rename(backup_db)
            print(f"  ✓ Renamed epstein.db → {backup_db.name}")
        else:
            print("  (no existing epstein.db)")
        
        if vector_dir.exists():
            backup_vec = BASE_PATH / f"vector_store.old_{ts}"
            vector_dir.rename(backup_vec)
            print(f"  ✓ Renamed vector_store/ → {backup_vec.name}")
        else:
            print("  (no existing vector_store)")
        
        print("\n  Next: run index to populate the new database from extracted_text:")
        print("    python run.py index")
        print("\n  This will take a while (one row per document, embeddings for searchable docs).")
        sys.exit(0)
    
    # Handle generate-thumbnails command
    if args.command == "generate-thumbnails":
        generate_all_thumbnails(max_workers=args.workers)
        sys.exit(0)
    
    # Step 0: Check/download FOIA files
    if args.command in ["download", "setup", "all"] or args.full_setup:
        setup_foia(force=force)
    
    # Step 1: Check/download court records
    if args.command in ["download", "setup", "all"] or args.full_setup:
        setup_court_records(force=force)
    
    # Step 1b: Check/download DOJ Disclosures (auto-detects existing Data Set folders)
    if args.command in ["download", "setup", "all"] or args.full_setup:
        # Parse --doj-datasets if provided
        doj_datasets = None
        if hasattr(args, 'doj_datasets') and args.doj_datasets:
            try:
                doj_datasets = [int(x.strip()) for x in args.doj_datasets.split(',')]
            except ValueError:
                print(f"⚠ Invalid --doj-datasets format: {args.doj_datasets}")
        setup_doj_disclosures(force=force, datasets=doj_datasets)
        
        if args.command == "download":
            print("\n✅ Download complete. Run 'python run.py setup' to index the files.")
            sys.exit(0)
    
    # Step 2: Extract text from PDFs and transcribe media
    # Enable maintenance mode for extraction and indexing operations
    maintenance_enabled = False
    if args.command in ["extract", "setup", "all"] or args.full_setup or args.reindex:
        enable_maintenance("Processing and indexing documents...")
        maintenance_enabled = True
    
    try:
        if args.command in ["extract", "setup", "all"] or args.full_setup:
            extract_all_media(force=force, max_workers=args.workers)
        
        # Step 3: Build search index
        if args.command in ["index", "setup", "all"] or args.full_setup or args.reindex:
            build_index(force=force)
    finally:
        # Always disable maintenance mode when done (or on error)
        if maintenance_enabled:
            disable_maintenance()
    
    # Step 4: Start server
    if args.command in ["server", "all"]:
        run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()


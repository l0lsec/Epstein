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
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

BASE_PATH = Path(__file__).parent
BACKEND_PATH = BASE_PATH / "backend"
SCRIPTS_PATH = BASE_PATH / "scripts"


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
        datasets: List of specific data set numbers to download (default: 9, 10, 11, 12)
    """
    print("\n" + "="*60)
    print("STEP 1b: CHECKING DOJ DISCLOSURES")
    print("="*60)
    
    doj_dir = BASE_PATH / "DOJ Disclosures"
    
    # Default to new data sets (9, 10, 11, 12) if not specified
    if datasets is None:
        datasets = [9, 10, 11, 12]
    
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
        
        if len(existing_datasets) == len(datasets) and total_files > 100:
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


def extract_all_media(force=False):
    """Extract text from PDFs and transcribe audio/video files"""
    print("\n" + "="*60)
    print("STEP 2: EXTRACTING TEXT & TRANSCRIBING MEDIA")
    print("="*60)
    
    # Show current status
    show_extraction_status()
    
    # Clean up stale entries first
    print("\n  Checking for stale entries...")
    cleanup_stale_documents()
    
    sys.path.insert(0, str(BACKEND_PATH))
    from extractor import MediaExtractor
    
    print("\n  Starting extraction (this may take a while)...")
    extractor = MediaExtractor(str(BASE_PATH))
    results = extractor.extract_all(max_workers=8, force=force)
    
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
    
    db_build_index(str(BASE_PATH), force=force)
    print("✓ Index built successfully")


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
  
DOJ Disclosures (Data Sets 9, 10, 11, 12 released January 2026):
  python run.py download                             # Download all sources including new data sets
  python run.py download --doj-datasets 9,10,11,12     # Download only specific data sets
  python scripts/download_doj_disclosures.py -d 9   # Download Data Set 9 only (standalone)
  
Adding new files (PDFs, audio, video):
  python run.py add /path/to/file.pdf                    # Add single PDF
  python run.py add /path/to/recording.mp4               # Add single video (transcribed)
  python run.py add /path/to/folder/                     # Add all supported files
  python run.py add /path/to/files --category "Evidence" # Add to specific category

Database maintenance:
  python run.py fix-fts                                  # Rebuild FTS5 full-text search index
  python run.py cleanup-db                               # Remove orphaned/duplicate entries

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
                        choices=["setup", "extract", "index", "server", "all", "add", "download", "fix-fts", "cleanup-db"],
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
        
        db = Database(str(db_path))
        db.rebuild_fts()
        print("✅ FTS5 index rebuilt successfully!")
        sys.exit(0)
    
    # Handle cleanup-db command
    if args.command == "cleanup-db":
        print("\n" + "="*60)
        print("DATABASE CLEANUP - Removing orphaned entries")
        print("="*60)
        
        import sqlite3
        db_path = BASE_PATH / "epstein.db"
        if not db_path.exists():
            print("✗ Database not found. Run 'python run.py setup' first.")
            sys.exit(1)
        
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
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
            
            confirm = input("\n  Delete these entries? [y/N]: ")
            if confirm.lower() == 'y':
                for doc_id, path, filename in orphaned:
                    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                conn.commit()
                print(f"  ✓ Deleted {len(orphaned)} orphaned entries")
                
                # Rebuild FTS index
                print("\n  🔄 Rebuilding FTS index...")
                from backend.database import Database
                db = Database(str(db_path))
                db.rebuild_fts()
                print("  ✓ FTS index rebuilt")
            else:
                print("  ⚠ Skipped deletion")
        else:
            print("  ✓ No orphaned entries found")
        
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
    
    # Step 0: Check/download FOIA files
    if args.command in ["download", "setup", "all"] or args.full_setup:
        setup_foia(force=force)
    
    # Step 1: Check/download court records
    if args.command in ["download", "setup", "all"] or args.full_setup:
        setup_court_records(force=force)
    
    # Step 1b: Check/download DOJ Disclosures (Data Sets 9, 10, 11, 12)
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
    if args.command in ["extract", "setup", "all"] or args.full_setup:
        extract_all_media(force=force)
    
    # Step 3: Build search index
    if args.command in ["index", "setup", "all"] or args.full_setup or args.reindex:
        build_index(force=force)
    
    # Step 4: Start server
    if args.command in ["server", "all"]:
        run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()


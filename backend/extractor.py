"""
Media Extraction Module
Extracts text from PDFs and transcribes audio/video files for indexing
"""

import os
import sys
import json
import hashlib
import platform
import subprocess
import threading
import signal
import time
from pathlib import Path
from typing import Generator, Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from tqdm import tqdm
import pdfplumber
import multiprocessing
import re
from dateutil import parser as date_parser


def extract_email_date(text: str) -> Optional[str]:
    """
    Extract date from email headers in document text.
    
    Looks for patterns like:
    - Date: Fri, 02 Oct 2020 13:26:21 +0000 (RFC 2822)
    - Sent: Tuesday, July 23, 2013 5:35 PM
    - Date: July 23, 2013
    - Sent: 07/23/2013
    - Date: 2013-07-23
    
    Returns:
        Date string in YYYY-MM-DD format, or None if no date found
    """
    if not text:
        return None
    
    # Only search the first 2000 characters for efficiency (email headers are at the top)
    search_text = text[:2000]
    
    # Patterns to match email date headers (order matters - more specific patterns first)
    patterns = [
        # RFC 2822 format: "Date: Fri, 02 Oct 2020 13:26:21 +0000"
        r'Date:\s*[A-Za-z]{3},?\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})',
        # "Sent: Tuesday, July 23, 2013 5:35 PM" or "Sent: July 23, 2013"
        r'Sent:\s*(?:[A-Za-z]+,?\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?)',
        # "Date: July 23, 2013" or "Date: 23 July 2013"
        r'Date:\s*(?:[A-Za-z]+,?\s+)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
        r'Date:\s*(?:[A-Za-z]+,?\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
        # "Sent: 07/23/2013" or "Date: 07/23/2013"
        r'(?:Sent|Date):\s*(\d{1,2}/\d{1,2}/\d{2,4})',
        # "Sent: 2013-07-23" or "Date: 2013-07-23" (ISO format)
        r'(?:Sent|Date):\s*(\d{4}-\d{2}-\d{2})',
        # "From: ... Date: ..." pattern common in email chains
        r'From:.*?(?:Sent|Date):\s*([A-Za-z]{3},?\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, search_text, re.IGNORECASE | re.DOTALL)
        if match:
            try:
                # Parse the matched date string
                parsed = date_parser.parse(match.group(1), fuzzy=True)
                # Return in YYYY-MM-DD format for consistent storage
                return parsed.strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                continue
    
    return None


# Global flag for skipping current file
_skip_current_file = False
_skip_lock = threading.Lock()


def _setup_skip_signal():
    """Setup signal handler for Ctrl+\\ (SIGQUIT) to skip current file"""
    def skip_handler(signum, frame):
        global _skip_current_file
        with _skip_lock:
            _skip_current_file = True
        print("\n  ⏭ Skip requested (Ctrl+\\)...")
    
    try:
        signal.signal(signal.SIGQUIT, skip_handler)
    except (AttributeError, ValueError):
        pass  # SIGQUIT not available on Windows


def _reset_skip_flag():
    """Reset the skip flag for the next file"""
    global _skip_current_file
    with _skip_lock:
        _skip_current_file = False


def _should_skip():
    """Check if current file should be skipped"""
    with _skip_lock:
        return _skip_current_file


def _extract_pdf_subprocess(args: tuple) -> Optional[Dict[str, Any]]:
    """
    Standalone function for PDF extraction that runs in a subprocess.
    This isolates segfaults from corrupted PDFs to the worker process only.
    
    Args:
        args: Tuple of (filepath_str, base_path_str, use_ocr)
    
    Returns:
        Extraction result dict or None on failure
    """
    filepath_str, base_path_str, use_ocr = args
    filepath = Path(filepath_str)
    base_path = Path(base_path_str)
    
    try:
        pages = []
        full_text = []
        used_ocr = False
        
        with pdfplumber.open(str(filepath)) as pdf:
            for page_num, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                
                # If no text but page has images, try OCR
                if not text.strip() and use_ocr:
                    try:
                        import pytesseract
                        pytesseract.get_tesseract_version()
                        if page.images:  # Page has images (likely scanned)
                            try:
                                from PIL import Image
                                import io
                                page_image = page.to_image(resolution=150)
                                pil_image = page_image.original
                                text = pytesseract.image_to_string(pil_image, lang='eng')
                                text = text.strip()
                                if text:
                                    used_ocr = True
                            except Exception:
                                pass
                    except Exception:
                        pass  # Tesseract not available
                
                if text.strip():
                    pages.append({
                        "page": page_num + 1,
                        "text": text.strip()
                    })
                    full_text.append(text.strip())
        
        # Get metadata
        relative_path = filepath.relative_to(base_path)
        
        # Decode URL-encoded filename
        from urllib.parse import unquote
        clean_name = unquote(filepath.name)
        
        # Generate file hash using RELATIVE path (must match _file_hash method)
        file_hash = hashlib.md5(f"{relative_path}_{filepath.stat().st_size}".encode()).hexdigest()
        
        # Categorize file (simplified version for subprocess)
        category, subcategory = _categorize_file_simple(filepath, base_path)
        
        result = {
            "id": file_hash,
            "filename": clean_name,
            "original_filename": filepath.name,
            "path": str(relative_path),
            "full_path": str(filepath),
            "category": category,
            "subcategory": subcategory,
            "file_type": "pdf",
            "page_count": len(pages),
            "pages": pages,
            "full_text": "\n\n".join(full_text),
            "char_count": sum(len(p["text"]) for p in pages),
            "has_content": len(full_text) > 0
        }
        
        if used_ocr:
            result["extraction_method"] = "ocr"
        
        return result
        
    except Exception as e:
        # Generate file hash for error result using RELATIVE path (must match _file_hash method)
        try:
            relative_path = filepath.relative_to(base_path)
            file_hash = hashlib.md5(f"{relative_path}_{filepath.stat().st_size}".encode()).hexdigest()
            from urllib.parse import unquote
            clean_name = unquote(filepath.name)
        except:
            file_hash = hashlib.md5(str(filepath).encode()).hexdigest()
            relative_path = filepath
            clean_name = filepath.name
            
        return {
            "id": file_hash,
            "filename": clean_name,
            "path": str(relative_path),
            "error": str(e),
            "has_content": False
        }


def _categorize_file_simple(filepath: Path, base_path: Path) -> tuple:
    """
    Simplified file categorization for subprocess use.
    Returns (category, subcategory) tuple.
    """
    import re
    from urllib.parse import unquote
    
    relative_path = filepath.relative_to(base_path)
    parts = relative_path.parts
    filename = unquote(filepath.name)
    
    category = "Unknown"
    subcategory = ""
    
    # EFTA files always go to DOJ Disclosures
    if filename.startswith("EFTA"):
        category = "DOJ Disclosures"
        # Try folder-based detection first
        path_str = str(filepath)
        folder_match = re.search(r'Data Set (\d+)', path_str)
        if folder_match:
            subcategory = f"Data Set {folder_match.group(1)}"
        else:
            # Fall back to EFTA number ranges
            match = re.search(r'EFTA(\d+)', filename)
            if match:
                file_num = int(match.group(1))
                if file_num <= 3158:
                    subcategory = "Data Set 1"
                elif file_num <= 3857:
                    subcategory = "Data Set 2"
                elif file_num <= 5704:
                    subcategory = "Data Set 3"
                elif file_num <= 8408:
                    subcategory = "Data Set 4"
                elif file_num <= 8528:
                    subcategory = "Data Set 5"
                elif file_num <= 9015:
                    subcategory = "Data Set 6"
                elif file_num <= 9675:
                    subcategory = "Data Set 7"
                elif file_num < 39025:
                    subcategory = "Data Set 8"
                elif file_num < 1262782:
                    subcategory = "Data Set 9"
                elif file_num < 2212883:
                    subcategory = "Data Set 10"
                elif file_num < 2730265:
                    subcategory = "Data Set 11"
                else:
                    subcategory = "Data Set 12"
            else:
                subcategory = "EFTA Documents"
        return category, subcategory
    
    if "DOJ Disclosures" in parts:
        category = "DOJ Disclosures"
        if "Flight" in filename:
            subcategory = "Flight Logs"
        elif "Contact" in filename:
            subcategory = "Contact Books"
        else:
            subcategory = "Other Documents"
    elif "FBI Vault" in parts:
        category = "FBI Vault"
        match = re.search(r'Part (\d+) of (\d+)', filename)
        if match:
            subcategory = f"Part {int(match.group(1)):02d} of {int(match.group(2)):02d}"
        else:
            subcategory = "FBI Records"
    elif "House Disclosures" in parts:
        category = "House Disclosures"
        try:
            hd_idx = parts.index("House Disclosures")
            if hd_idx + 1 < len(parts):
                folder_name = parts[hd_idx + 1]
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4', '.jpg', '.tif')):
                    subcategory = folder_name
        except:
            subcategory = "Other Documents"
    elif "CourtRecords" in parts:
        category = "Court Records"
        try:
            court_idx = parts.index("CourtRecords")
            if court_idx + 1 < len(parts) - 1:
                subcategory = parts[court_idx + 1]
        except:
            subcategory = "Legal_Filings"
    elif "FOIA" in parts:
        category = "FOIA"
        try:
            foia_idx = parts.index("FOIA")
            if foia_idx + 1 < len(parts):
                folder_name = parts[foia_idx + 1]
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4')):
                    subcategory = folder_name
        except:
            subcategory = "Other FOIA Documents"
    
    return category, subcategory


# Supported file types
PDF_EXTENSIONS = {'.pdf'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.tif', '.tiff', '.png', '.bmp', '.gif'}
AUDIO_EXTENSIONS = {'.wav', '.mp3', '.m4a', '.ogg', '.flac', '.aac', '.wma'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv'}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# Directories to ignore during extraction
IGNORED_DIRS = {
    # Python virtual environments
    'venv', '.venv', 'env', '.env',
    # Node.js
    'node_modules',
    # Version control
    '.git', '.svn', '.hg',
    # Python cache
    '__pycache__', '.pytest_cache',
    # Our output/system directories
    'extracted_text', 'transcripts',
    'thumbnails',      # Thumbnail cache
    'vector_store',    # Vector embeddings
    'frontend',        # Web frontend
    'backend',         # Python backend code
    'scripts',         # Utility scripts
    'logs',            # Log files
    'temp', 'tmp',     # Temporary files
    # IDE directories
    '.cursor', '.vscode', '.idea',
}


class PDFExtractor:
    """Extracts text content from PDF files"""
    
    def __init__(self, base_path: str):
        self.base_path = Path(base_path)
        self.extracted_dir = self.base_path / "extracted_text"
        self.extracted_dir.mkdir(exist_ok=True)
        self.index_file = self.extracted_dir / "index.json"
        self.failed_files_log = self.extracted_dir / "failed_pdf_files.json"
        self.index = self._load_index()
        self.failed_files = self._load_failed_files()
        self._tesseract_available = None
    
    def _load_failed_files(self) -> Dict[str, Any]:
        """Load log of previously failed files"""
        if self.failed_files_log.exists():
            with open(self.failed_files_log, 'r') as f:
                return json.load(f)
        return {"files": [], "count": 0}
    
    def _save_failed_file(self, filepath: Path, error: str):
        """Log a failed file for later review"""
        from datetime import datetime
        # Check if already logged
        existing_paths = [f["path"] for f in self.failed_files["files"]]
        try:
            rel_path = str(filepath.relative_to(self.base_path))
        except:
            rel_path = str(filepath)
        
        if rel_path not in existing_paths:
            try:
                size_mb = round(filepath.stat().st_size / 1024 / 1024, 2)
            except:
                size_mb = 0
            
            self.failed_files["files"].append({
                "path": rel_path,
                "filename": filepath.name,
                "error": error[:200],  # Truncate long errors
                "size_mb": size_mb,
                "timestamp": datetime.now().isoformat()
            })
            self.failed_files["count"] = len(self.failed_files["files"])
            with open(self.failed_files_log, 'w') as f:
                json.dump(self.failed_files, f, indent=2)
    
    def _check_tesseract(self) -> bool:
        """Check if Tesseract OCR is available"""
        if self._tesseract_available is not None:
            return self._tesseract_available
        
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            self._tesseract_available = True
        except Exception:
            self._tesseract_available = False
        
        return self._tesseract_available
    
    def _ocr_pdf_page(self, page) -> str:
        """Extract text from a PDF page using OCR"""
        try:
            import pytesseract
            from PIL import Image
            import io
            
            # Convert page to image
            page_image = page.to_image(resolution=150)
            pil_image = page_image.original
            
            # Perform OCR
            text = pytesseract.image_to_string(pil_image, lang='eng')
            return text.strip()
        except Exception as e:
            print(f"    OCR error: {e}")
            return ""
    
    def _load_index(self) -> Dict[str, Any]:
        """Load existing extraction index, rebuilding from files if corrupted"""
        if self.index_file.exists():
            try:
                with open(self.index_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                print(f"  ⚠ Index file corrupted: {e}")
                print(f"  🔄 Rebuilding index from extracted files...")
                return self._rebuild_index_from_files()
        return {"files": {}, "stats": {"total": 0, "processed": 0, "failed": 0}}
    
    def _rebuild_index_from_files(self) -> Dict[str, Any]:
        """Rebuild index by scanning all extracted JSON files"""
        index = {"files": {}, "stats": {"total": 0, "processed": 0, "failed": 0}}
        
        # Find all extracted JSON files (excluding index files)
        # Use os.scandir for better performance with large directories
        print(f"  📂 Scanning extracted files directory...")
        excluded_files = {"index.json", "image_index.json", "media_index.json", 
                          "failed_pdf_files.json", "failed_media_files.json",
                          "index.json.corrupted"}
        
        json_files = []
        try:
            with os.scandir(self.extracted_dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.endswith('.json') and entry.name not in excluded_files:
                        json_files.append(entry.path)
        except Exception as e:
            print(f"  ⚠ Error scanning directory: {e}")
            return index
        
        total_files = len(json_files)
        print(f"  📂 Found {total_files:,} extracted files to rebuild index from...")
        
        rebuilt_count = 0
        errors = 0
        last_progress = 0
        
        for i, json_path in enumerate(json_files):
            try:
                with open(json_path, 'r') as f:
                    data = json.load(f)
                
                if data.get("id") and data.get("has_content"):
                    index["files"][data["id"]] = {
                        "filename": data.get("filename", ""),
                        "path": data.get("path", ""),
                        "category": data.get("category", "Unknown"),
                        "subcategory": data.get("subcategory", ""),
                        "page_count": data.get("page_count", 0),
                        "char_count": data.get("char_count", 0)
                    }
                    rebuilt_count += 1
            except Exception:
                errors += 1
                continue  # Skip corrupted individual files
            
            # Show progress every 10%
            progress = int((i + 1) / total_files * 100)
            if progress >= last_progress + 10:
                print(f"  📊 Rebuilding: {progress}% ({i+1:,}/{total_files:,})")
                last_progress = progress
        
        index["stats"]["processed"] = rebuilt_count
        print(f"  ✓ Rebuilt index with {rebuilt_count:,} files ({errors} errors)")
        
        # Backup the corrupted file first
        corrupted_backup = self.index_file.with_suffix('.json.corrupted')
        if self.index_file.exists():
            try:
                import shutil
                shutil.move(str(self.index_file), str(corrupted_backup))
                print(f"  📦 Corrupted index backed up to: {corrupted_backup.name}")
            except:
                pass
        
        # Save the rebuilt index directly (can't use _save_index as self.index isn't set yet)
        with open(self.index_file, 'w') as f:
            json.dump(index, f, indent=2)
        
        return index
    
    def _save_index(self):
        """Save extraction index"""
        with open(self.index_file, 'w') as f:
            json.dump(self.index, f, indent=2)
    
    def _file_hash(self, filepath: Path) -> str:
        """Generate hash for file to detect changes
        
        Uses relative path from base_path to ensure consistent hashes
        across different environments (local vs server).
        """
        relative_path = filepath.relative_to(self.base_path)
        return hashlib.md5(f"{relative_path}_{filepath.stat().st_size}".encode()).hexdigest()
    
    def _clean_filename(self, filename: str) -> str:
        """Decode URL-encoded filenames and clean them"""
        from urllib.parse import unquote
        return unquote(filename)
    
    def _get_efta_dataset(self, filename: str, filepath: Path = None) -> str:
        """Determine which dataset an EFTA file belongs to.
        
        Uses folder-based detection first (most reliable), then falls back
        to EFTA number ranges.
        
        Data Set ranges (by EFTA number):
        - Data Set 1: 1-3158
        - Data Set 2: 3159-3857
        - Data Set 3: 3858-5704
        - Data Set 4: 5705-8408
        - Data Set 5: 8409-8528
        - Data Set 6: 8529-9015
        - Data Set 7: 9016-9675
        - Data Set 8: 9676-39024
        - Data Set 9: 39025-1262781 (released January 2026)
        - Data Set 10: 1262782-2212882 (released January 2026)
        - Data Set 11: 2212883-2730264 (released January 2026)
        - Data Set 12: 2730265+ (released January 2026)
        """
        import re
        
        # First, try folder-based detection (works for all data sets)
        if filepath is not None:
            try:
                path_str = str(filepath)
                folder_match = re.search(r'Data Set (\d+)', path_str)
                if folder_match:
                    return f"Data Set {folder_match.group(1)}"
            except:
                pass
        
        # Fall back to EFTA number-based detection
        match = re.search(r'EFTA(\d+)', filename)
        if not match:
            return "EFTA Documents"
        
        file_num = int(match.group(1))
        
        # Dataset ranges per DOJ website
        if file_num <= 3158:
            return "Data Set 1"
        elif file_num <= 3857:
            return "Data Set 2"
        elif file_num <= 5704:
            return "Data Set 3"
        elif file_num <= 8408:
            return "Data Set 4"
        elif file_num <= 8528:
            return "Data Set 5"
        elif file_num <= 9015:
            return "Data Set 6"
        elif file_num <= 9675:
            return "Data Set 7"
        elif file_num < 39025:
            return "Data Set 8"
        elif file_num < 1262782:
            return "Data Set 9"
        elif file_num < 2212883:
            return "Data Set 10"
        elif file_num < 2730265:
            return "Data Set 11"
        else:
            return "Data Set 12"
    
    def _categorize_file(self, filepath: Path) -> Dict[str, str]:
        """Categorize file based on path and filename"""
        relative_path = filepath.relative_to(self.base_path)
        parts = relative_path.parts
        filename = self._clean_filename(filepath.name)
        
        category = "Unknown"
        subcategory = ""
        
        # EFTA files always go to DOJ Disclosures regardless of folder location
        if filename.startswith("EFTA"):
            category = "DOJ Disclosures"
            subcategory = self._get_efta_dataset(filename, filepath)
            return {"category": category, "subcategory": subcategory}
        
        if "DOJ Disclosures" in parts:
            category = "DOJ Disclosures"
            filename = self._clean_filename(filepath.name)
            if filename.startswith("EFTA"):
                # Categorize by dataset number
                subcategory = self._get_efta_dataset(filename)
            elif "Flight" in filename:
                subcategory = "Flight Logs"
            elif "Contact" in filename:
                subcategory = "Contact Books"
            elif "OIG" in filename:
                subcategory = "OIG Reports"
            elif "Memorandum" in filename:
                subcategory = "DOJ/FBI Memoranda"
            elif "Report" in filename:
                subcategory = "DOJ Reports"
            elif "Letter" in filename:
                subcategory = "Correspondence"
            elif "Masseuse" in filename:
                subcategory = "Masseuse Lists"
            elif "Evidence" in filename:
                subcategory = "Evidence Lists"
            elif "Interview" in filename or "Maxwell" in filename or "Proffer" in filename:
                subcategory = "Maxwell Proffer"
            else:
                subcategory = "Other Documents"
        elif "FBI Vault" in parts:
            category = "FBI Vault"
            filename = self._clean_filename(filepath.name)
            # Extract part number from filename like "Jeffrey Epstein Part 01 of 08.pdf"
            import re
            match = re.search(r'Part (\d+) of (\d+)', filename)
            if match:
                part_num = int(match.group(1))
                total_parts = int(match.group(2))
                subcategory = f"Part {part_num:02d} of {total_parts:02d}"
            else:
                subcategory = "FBI Records"
        elif "House Disclosures" in parts:
            category = "House Disclosures"
            # Subcategorize by folder structure (e.g., Prod 01_20250822/VOL00001/IMAGES)
            subcategory = self._get_house_disclosures_subcategory(relative_path)
        elif "CourtRecords" in parts:
            category = "Court Records"
            # Get subcategory from subfolder name (case name)
            subcategory = self._get_court_case_name(relative_path)
        elif "FOIA" in parts:
            category = "FOIA"
            # Get subcategory from subfolder name (agency name)
            subcategory = self._get_foia_subcategory(relative_path)
        
        return {"category": category, "subcategory": subcategory}
    
    def _get_house_disclosures_subcategory(self, relative_path: Path) -> str:
        """Extract House Disclosures subcategory from path (production folder)
        
        Structure: House Disclosures / Prod 01_20250822 / VOL00001 / IMAGES / IMAGES001 / file.jpg
        Subcategory should be the production folder (e.g., "Prod 01_20250822")
        """
        parts = relative_path.parts
        
        # Find the House Disclosures folder and get the next subfolder (production)
        try:
            hd_idx = parts.index("House Disclosures")
            if hd_idx + 1 < len(parts):
                folder_name = parts[hd_idx + 1]
                # Only use folder name if it's not the file itself
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4', '.jpg', '.tif', '.dat', '.opt')):
                    return folder_name
        except (ValueError, IndexError):
            pass
        
        return "Other Documents"
    
    def _get_foia_subcategory(self, relative_path: Path) -> str:
        """Extract FOIA subcategory from path (agency/source folder)"""
        parts = relative_path.parts
        
        # Find the FOIA folder and get the next subfolder
        try:
            foia_idx = parts.index("FOIA")
            if foia_idx + 1 < len(parts):
                folder_name = parts[foia_idx + 1]
                # Only use folder name if it's not the file itself
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4')):
                    return folder_name
        except (ValueError, IndexError):
            pass
        
        # Fallback: try to determine source from filename patterns
        filename = self._clean_filename(relative_path.name)
        
        if "BOP" in filename or "Bureau of Prisons" in filename:
            return "Federal Bureau of Prisons (BOP)"
        elif "TECS" in filename or "Travel" in filename or "Aircraft" in filename:
            return "Customs and Border Protection (CBP)"
        elif filename.startswith("Epstein Part") and "Redacted" not in filename:
            return "Federal Bureau of Investigation (FBI)"
        elif filename.startswith("Epstein Travel"):
            return "Travel Records"
        elif "06CF009454" in filename or "Redacted" in filename or "Stac" in filename or "Phone" in filename:
            return "Florida"
        
        return "Other FOIA Documents"
    
    def _get_court_case_name(self, relative_path: Path) -> str:
        """Extract court case name from path for subcategorization"""
        parts = relative_path.parts
        
        # Find the CourtRecords folder and get the next subfolder
        try:
            court_idx = parts.index("CourtRecords")
            if court_idx + 1 < len(parts) - 1:  # There's a subfolder after CourtRecords
                folder_name = parts[court_idx + 1]
                # Use folder name as subcategory (it's already well-formatted)
                return folder_name
        except (ValueError, IndexError):
            pass
        
        # Fallback: try to determine case from filename patterns
        filename = self._clean_filename(relative_path.name)
        
        # Map common file patterns to case names
        case_patterns = {
            "Bryant": "Bryant_v_Indyke",
            "Davies": "Davies_v_Indyke", 
            "Maxwell": "US_v_Maxwell",
            "Epstein": "US_v_Epstein",
            "Noel": "US_v_Noel",
            "Doe": "Doe_v_Indyke",
            "VE": "VE_v_Nine_East_71st",
            "CL": "CL_v_Epstein",
            "Florida": "CA_Florida_Holdings_v_Aronberg",
        }
        
        for pattern, case_name in case_patterns.items():
            if pattern.lower() in filename.lower():
                return case_name
        
        return "Legal_Filings"
    
    def extract_pdf(self, filepath: Path, use_ocr: bool = True) -> Optional[Dict[str, Any]]:
        """Extract text from a single PDF file, with OCR fallback for scanned documents"""
        try:
            pages = []
            full_text = []
            used_ocr = False
            
            with pdfplumber.open(str(filepath)) as pdf:
                total_pages = len(pdf.pages)
                
                for page_num, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    
                    # If no text but page has images, try OCR
                    if not text.strip() and use_ocr and self._check_tesseract():
                        if page.images:  # Page has images (likely scanned)
                            text = self._ocr_pdf_page(page)
                            if text:
                                used_ocr = True
                    
                    if text.strip():
                        pages.append({
                            "page": page_num + 1,
                            "text": text.strip()
                        })
                        full_text.append(text.strip())
            
            # Get metadata
            relative_path = filepath.relative_to(self.base_path)
            clean_name = self._clean_filename(filepath.name)
            categorization = self._categorize_file(filepath)
            
            result = {
                "id": self._file_hash(filepath),
                "filename": clean_name,
                "original_filename": filepath.name,
                "path": str(relative_path),
                "full_path": str(filepath),
                "category": categorization["category"],
                "subcategory": categorization["subcategory"],
                "file_type": "pdf",
                "page_count": len(pages),
                "pages": pages,
                "full_text": "\n\n".join(full_text),
                "char_count": sum(len(p["text"]) for p in pages),
                "has_content": len(full_text) > 0
            }
            
            if used_ocr:
                result["extraction_method"] = "ocr"
            
            return result
            
        except Exception as e:
            return {
                "id": self._file_hash(filepath),
                "filename": self._clean_filename(filepath.name),
                "path": str(filepath.relative_to(self.base_path)),
                "error": str(e),
                "has_content": False
            }
    
    def find_all_pdfs(self) -> Generator[Path, None, None]:
        """Find all PDF files in the base path, excluding ignored directories"""
        for root, dirs, files in os.walk(self.base_path):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            
            for file in files:
                if file.lower().endswith('.pdf'):
                    yield Path(root) / file
    
    def extract_all(self, max_workers: int = 4, force: bool = False, progress_callback=None) -> Dict[str, Any]:
        """Extract text from all PDFs using process isolation
        
        Uses ProcessPoolExecutor to isolate each PDF extraction in a subprocess.
        This prevents segfaults from corrupted PDFs from crashing the entire extraction.
        
        Args:
            max_workers: Number of parallel workers
            force: Force re-extraction of all files
            progress_callback: Optional callback(current, total) for progress updates
        """
        pdfs = list(self.find_all_pdfs())
        print(f"Found {len(pdfs)} PDF files")
        
        results = {"success": 0, "failed": 0, "skipped": 0}
        
        # Filter already processed files unless force
        to_process = []
        for pdf in pdfs:
            file_hash = self._file_hash(pdf)
            if force or file_hash not in self.index["files"]:
                to_process.append(pdf)
            else:
                results["skipped"] += 1
        
        print(f"Processing {len(to_process)} new files ({results['skipped']} already processed)")
        
        if not to_process:
            return results
        
        # Check if tesseract is available for OCR
        use_ocr = self._check_tesseract()
        
        processed_count = 0
        save_interval = 500  # Save index every 500 files (more frequent for crash recovery)
        last_save_count = 0
        file_timeout = 120  # 2 minute timeout per file
        
        # Prepare arguments for subprocess function
        # Convert Path to string for pickling
        base_path_str = str(self.base_path)
        process_args = [(str(pdf), base_path_str, use_ocr) for pdf in to_process]
        
        print(f"  🔒 Using process isolation (max {max_workers} workers, {file_timeout}s timeout)")
        print(f"  💡 Corrupted PDFs will only crash their worker, not the entire process")
        
        # Use ProcessPoolExecutor for crash isolation
        # maxtasksperchild=50 restarts workers periodically to prevent memory leaks
        with ProcessPoolExecutor(max_workers=max_workers, mp_context=multiprocessing.get_context('spawn')) as executor:
            # Submit all jobs
            futures = {executor.submit(_extract_pdf_subprocess, args): to_process[i] for i, args in enumerate(process_args)}
            
            for future in tqdm(as_completed(futures), total=len(to_process), desc="Extracting"):
                pdf = futures[future]
                processed_count += 1
                
                # Call progress callback if provided
                if progress_callback:
                    try:
                        progress_callback(processed_count, len(to_process))
                    except:
                        pass
                
                try:
                    result = future.result(timeout=file_timeout)
                    if result and result.get("has_content"):
                        # Save extracted text
                        output_file = self.extracted_dir / f"{result['id']}.json"
                        with open(output_file, 'w') as f:
                            json.dump(result, f)
                        
                        self.index["files"][result["id"]] = {
                            "filename": result["filename"],
                            "path": result["path"],
                            "category": result.get("category", "Unknown"),
                            "subcategory": result.get("subcategory", ""),
                            "page_count": result.get("page_count", 0),
                            "char_count": result.get("char_count", 0)
                        }
                        results["success"] += 1
                    elif result and result.get("error"):
                        # Log the error but continue
                        results["failed"] += 1
                    else:
                        results["failed"] += 1
                except FuturesTimeoutError:
                    print(f"\n  ⏱ Timeout ({file_timeout}s): {pdf.name}")
                    self._save_failed_file(pdf, f"Timeout after {file_timeout}s")
                    results["failed"] += 1
                except multiprocessing.TimeoutError:
                    print(f"\n  ⏱ Timeout ({file_timeout}s): {pdf.name}")
                    self._save_failed_file(pdf, f"Timeout after {file_timeout}s")
                    results["failed"] += 1
                except Exception as e:
                    # This catches BrokenProcessPool and other subprocess errors
                    error_msg = str(e)
                    if "BrokenProcessPool" in error_msg or "segmentation" in error_msg.lower():
                        print(f"\n  💥 Worker crashed on {pdf.name} (corrupted PDF?)")
                        self._save_failed_file(pdf, "Worker crashed (likely corrupted PDF)")
                    else:
                        print(f"\n  ✗ Error processing {pdf.name}: {e}")
                        self._save_failed_file(pdf, error_msg)
                    results["failed"] += 1
                
                # Periodically save index to prevent progress loss on crash
                if processed_count - last_save_count >= save_interval:
                    self.index["stats"]["total"] = len(pdfs)
                    self.index["stats"]["processed"] = results["success"] + results["skipped"]
                    self.index["stats"]["failed"] = results["failed"]
                    self._save_index()
                    last_save_count = processed_count
                    print(f"\n  💾 Checkpoint saved ({processed_count:,} files processed)")
        
        # Final save
        self.index["stats"]["total"] = len(pdfs)
        self.index["stats"]["processed"] = results["success"] + results["skipped"]
        self.index["stats"]["failed"] = results["failed"]
        self._save_index()
        
        # Print summary of failed files
        if self.failed_files["count"] > 0:
            print(f"\n  ⚠ {self.failed_files['count']} PDFs failed - see: {self.failed_files_log}")
        
        return results


class ImageExtractor:
    """Extracts text from images using OCR (Tesseract)"""
    
    def __init__(self, base_path: str):
        self.base_path = Path(base_path)
        self.extracted_dir = self.base_path / "extracted_text"
        self.extracted_dir.mkdir(exist_ok=True)
        self.index_file = self.extracted_dir / "image_index.json"
        self.index = self._load_index()
        self._tesseract_available = None
    
    def _check_tesseract(self) -> bool:
        """Check if Tesseract OCR is available"""
        if self._tesseract_available is not None:
            return self._tesseract_available
        
        try:
            import pytesseract
            # Try to get tesseract version to verify it's installed
            pytesseract.get_tesseract_version()
            self._tesseract_available = True
        except Exception:
            self._tesseract_available = False
        
        return self._tesseract_available
    
    def _load_index(self) -> Dict[str, Any]:
        """Load existing extraction index"""
        if self.index_file.exists():
            with open(self.index_file, 'r') as f:
                return json.load(f)
        return {"files": {}, "stats": {"total": 0, "processed": 0, "failed": 0}}
    
    def _save_index(self):
        """Save extraction index"""
        with open(self.index_file, 'w') as f:
            json.dump(self.index, f, indent=2)
    
    def _file_hash(self, filepath: Path) -> str:
        """Generate hash for file to detect changes
        
        Uses relative path from base_path to ensure consistent hashes
        across different environments (local vs server).
        """
        relative_path = filepath.relative_to(self.base_path)
        return hashlib.md5(f"{relative_path}_{filepath.stat().st_size}".encode()).hexdigest()
    
    def _clean_filename(self, filename: str) -> str:
        """Decode URL-encoded filenames and clean them"""
        from urllib.parse import unquote
        return unquote(filename)
    
    def _get_efta_dataset(self, filename: str, filepath: Path = None) -> str:
        """Determine which dataset an EFTA file belongs to.
        
        Uses folder-based detection first (most reliable), then falls back
        to EFTA number ranges.
        
        Data Set ranges (by EFTA number):
        - Data Set 1: 1-3158
        - Data Set 2: 3159-3857
        - Data Set 3: 3858-5704
        - Data Set 4: 5705-8408
        - Data Set 5: 8409-8528
        - Data Set 6: 8529-9015
        - Data Set 7: 9016-9675
        - Data Set 8: 9676-39024
        - Data Set 9: 39025-1262781 (released January 2026)
        - Data Set 10: 1262782-2212882 (released January 2026)
        - Data Set 11: 2212883-2730264 (released January 2026)
        - Data Set 12: 2730265+ (released January 2026)
        """
        import re
        
        # First, try folder-based detection (works for all data sets)
        if filepath is not None:
            try:
                path_str = str(filepath)
                folder_match = re.search(r'Data Set (\d+)', path_str)
                if folder_match:
                    return f"Data Set {folder_match.group(1)}"
            except:
                pass
        
        # Fall back to EFTA number-based detection
        match = re.search(r'EFTA(\d+)', filename)
        if not match:
            return "EFTA Documents"
        
        file_num = int(match.group(1))
        
        # Dataset ranges per DOJ website
        if file_num <= 3158:
            return "Data Set 1"
        elif file_num <= 3857:
            return "Data Set 2"
        elif file_num <= 5704:
            return "Data Set 3"
        elif file_num <= 8408:
            return "Data Set 4"
        elif file_num <= 8528:
            return "Data Set 5"
        elif file_num <= 9015:
            return "Data Set 6"
        elif file_num <= 9675:
            return "Data Set 7"
        elif file_num < 39025:
            return "Data Set 8"
        elif file_num < 1262782:
            return "Data Set 9"
        elif file_num < 2212883:
            return "Data Set 10"
        elif file_num < 2730265:
            return "Data Set 11"
        else:
            return "Data Set 12"
    
    def _categorize_file(self, filepath: Path) -> Dict[str, str]:
        """Categorize file based on path and filename"""
        relative_path = filepath.relative_to(self.base_path)
        parts = relative_path.parts
        filename = self._clean_filename(filepath.name)
        
        category = "Unknown"
        subcategory = "Scanned Document"
        
        # EFTA files always go to DOJ Disclosures regardless of folder location
        if filename.startswith("EFTA"):
            category = "DOJ Disclosures"
            subcategory = self._get_efta_dataset(filename, filepath)
            return {"category": category, "subcategory": subcategory}
        
        # Categorize based on folder structure
        if "DOJ Disclosures" in parts:
            category = "DOJ Disclosures"
            subcategory = "Scanned Documents"
        elif "FBI Vault" in parts:
            category = "FBI Vault"
            # Extract part number from filename
            import re
            match = re.search(r'Part (\d+) of (\d+)', filename)
            if match:
                part_num = int(match.group(1))
                total_parts = int(match.group(2))
                subcategory = f"Part {part_num:02d} of {total_parts:02d}"
            else:
                subcategory = "FBI Records"
        elif "House Disclosures" in parts:
            category = "House Disclosures"
            # Get production folder as subcategory
            subcategory = self._get_house_disclosures_subcategory(relative_path)
        elif "CourtRecords" in parts:
            category = "Court Records"
            subcategory = self._get_court_case_name(relative_path)
        elif "FOIA" in parts:
            category = "FOIA"
            subcategory = self._get_foia_subcategory(relative_path)
        else:
            # Use parent folder as category, but skip ignored directories
            if len(parts) > 1 and parts[0] not in IGNORED_DIRS:
                category = parts[0]
        
        return {"category": category, "subcategory": subcategory}
    
    def _get_house_disclosures_subcategory(self, relative_path: Path) -> str:
        """Extract House Disclosures subcategory from path"""
        parts = relative_path.parts
        try:
            hd_idx = parts.index("House Disclosures")
            if hd_idx + 1 < len(parts):
                folder_name = parts[hd_idx + 1]
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4', '.jpg', '.tif', '.dat', '.opt')):
                    return folder_name
        except (ValueError, IndexError):
            pass
        return "Other Documents"
    
    def _get_court_case_name(self, relative_path: Path) -> str:
        """Extract court case name from path"""
        parts = relative_path.parts
        try:
            court_idx = parts.index("CourtRecords")
            if court_idx + 1 < len(parts) - 1:
                return parts[court_idx + 1]
        except (ValueError, IndexError):
            pass
        return "Legal_Filings"
    
    def _get_foia_subcategory(self, relative_path: Path) -> str:
        """Extract FOIA subcategory from path"""
        parts = relative_path.parts
        try:
            foia_idx = parts.index("FOIA")
            if foia_idx + 1 < len(parts):
                folder_name = parts[foia_idx + 1]
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4', '.jpg', '.tif')):
                    return folder_name
        except (ValueError, IndexError):
            pass
        
        # Fallback: try to determine source from filename patterns
        filename = self._clean_filename(relative_path.name)
        
        if "BOP" in filename or "Bureau of Prisons" in filename:
            return "Federal Bureau of Prisons (BOP)"
        elif "TECS" in filename or "Travel" in filename or "Aircraft" in filename:
            return "Customs and Border Protection (CBP)"
        elif filename.startswith("Epstein Part") and "Redacted" not in filename:
            return "Federal Bureau of Investigation (FBI)"
        elif filename.startswith("Epstein Travel"):
            return "Travel Records"
        elif "06CF009454" in filename or "Redacted" in filename or "Stac" in filename or "Phone" in filename:
            return "Florida"
        
        return "Other FOIA Documents"
    
    def extract_image(self, filepath: Path) -> Optional[Dict[str, Any]]:
        """Extract text from a single image using OCR"""
        try:
            import pytesseract
            from PIL import Image
            
            # Open and process image
            with Image.open(filepath) as img:
                # Convert to RGB if necessary (for RGBA images)
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                
                # Perform OCR
                text = pytesseract.image_to_string(img, lang='eng')
                text = text.strip()
            
            # Get metadata
            relative_path = filepath.relative_to(self.base_path)
            clean_name = self._clean_filename(filepath.name)
            categorization = self._categorize_file(filepath)
            
            return {
                "id": self._file_hash(filepath),
                "filename": clean_name,
                "original_filename": filepath.name,
                "path": str(relative_path),
                "full_path": str(filepath),
                "category": categorization["category"],
                "subcategory": categorization["subcategory"],
                "file_type": "image",
                "image_format": filepath.suffix.lower().lstrip('.'),
                "full_text": text,
                "char_count": len(text),
                "has_content": len(text) > 10  # Require at least some text
            }
            
        except Exception as e:
            return {
                "id": self._file_hash(filepath),
                "filename": self._clean_filename(filepath.name),
                "path": str(filepath.relative_to(self.base_path)),
                "error": str(e),
                "has_content": False
            }
    
    def find_all_images(self) -> Generator[Path, None, None]:
        """Find all image files in the base path, excluding ignored directories"""
        for root, dirs, files in os.walk(self.base_path):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            
            for file in files:
                ext = Path(file).suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    yield Path(root) / file
    
    def extract_all(self, max_workers: int = 4, force: bool = False, progress_callback=None) -> Dict[str, Any]:
        """Extract text from all images using OCR
        
        Args:
            max_workers: Number of parallel workers
            force: Force re-extraction of all files
            progress_callback: Optional callback(current, total) for progress updates
        """
        # Check if Tesseract is available
        if not self._check_tesseract():
            print("⚠ Tesseract OCR not available. Install with:")
            print("  macOS: brew install tesseract")
            print("  Ubuntu: sudo apt install tesseract-ocr")
            print("  Then: pip install pytesseract pillow")
            return {"success": 0, "failed": 0, "skipped": 0}
        
        images = list(self.find_all_images())
        
        if not images:
            print("No image files found")
            return {"success": 0, "failed": 0, "skipped": 0}
        
        print(f"Found {len(images)} image files")
        
        results = {"success": 0, "failed": 0, "skipped": 0}
        
        # Filter already processed files unless force
        to_process = []
        for img in images:
            file_hash = self._file_hash(img)
            if force or file_hash not in self.index["files"]:
                to_process.append(img)
            else:
                results["skipped"] += 1
        
        print(f"Processing {len(to_process)} new files ({results['skipped']} already processed)")
        
        # Process with thread pool
        processed_count = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.extract_image, img): img for img in to_process}
            
            for future in tqdm(as_completed(futures), total=len(to_process), desc="OCR Processing"):
                img = futures[future]
                processed_count += 1
                
                # Call progress callback if provided
                if progress_callback:
                    try:
                        progress_callback(processed_count, len(to_process))
                    except:
                        pass
                
                try:
                    result = future.result()
                    if result and result.get("has_content"):
                        # Save extracted text
                        output_file = self.extracted_dir / f"{result['id']}.json"
                        with open(output_file, 'w') as f:
                            json.dump(result, f)
                        
                        self.index["files"][result["id"]] = {
                            "filename": result["filename"],
                            "path": result["path"],
                            "category": result.get("category", "Unknown"),
                            "subcategory": result.get("subcategory", ""),
                            "image_format": result.get("image_format", ""),
                            "char_count": result.get("char_count", 0)
                        }
                        results["success"] += 1
                    else:
                        results["failed"] += 1
                except Exception as e:
                    print(f"Error processing {img}: {e}")
                    results["failed"] += 1
        
        # Update stats
        self.index["stats"]["total"] = len(images)
        self.index["stats"]["processed"] = results["success"] + results["skipped"]
        self.index["stats"]["failed"] = results["failed"]
        self._save_index()
        
        return results


class AudioVideoExtractor:
    """Transcribes audio and video files using OpenAI Whisper with local fallback"""
    
    def __init__(self, base_path: str, api_key: str = None):
        self.base_path = Path(base_path)
        self.extracted_dir = self.base_path / "extracted_text"
        self.extracted_dir.mkdir(exist_ok=True)
        self.index_file = self.extracted_dir / "media_index.json"
        self.failed_files_log = self.extracted_dir / "failed_media_files.json"
        self.index = self._load_index()
        self.failed_files = self._load_failed_files()
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = None
        self._local_model = None
        self._mlx_model = None
        self._index_lock = threading.Lock()  # Thread-safe index saves
        self._model_lock = threading.Lock()  # Thread-safe model loading
    
    def _load_failed_files(self) -> Dict[str, Any]:
        """Load log of previously failed files"""
        if self.failed_files_log.exists():
            with open(self.failed_files_log, 'r') as f:
                return json.load(f)
        return {"files": [], "count": 0}
    
    def _save_failed_file(self, filepath: Path, error: str):
        """Log a failed file for later review"""
        from datetime import datetime
        with self._index_lock:
            # Check if already logged
            existing_paths = [f["path"] for f in self.failed_files["files"]]
            rel_path = str(filepath.relative_to(self.base_path))
            if rel_path not in existing_paths:
                self.failed_files["files"].append({
                    "path": rel_path,
                    "filename": filepath.name,
                    "error": error,
                    "size_mb": round(filepath.stat().st_size / 1024 / 1024, 2),
                    "timestamp": datetime.now().isoformat()
                })
                self.failed_files["count"] = len(self.failed_files["files"])
                with open(self.failed_files_log, 'w') as f:
                    json.dump(self.failed_files, f, indent=2)
    
    def _validate_media_file(self, filepath: Path) -> tuple[bool, str]:
        """Quick validation using ffprobe to check if file is valid media (< 2 seconds)"""
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration,format_name',
                 '-of', 'json', str(filepath)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return False, f"ffprobe error: {result.stderr.strip()[:100]}"
            
            import json as json_module
            data = json_module.loads(result.stdout)
            if not data.get("format"):
                return False, "No media format detected"
            
            duration = data["format"].get("duration")
            if duration and float(duration) < 0.1:
                return False, "File too short (< 0.1s)"
            
            return True, "OK"
        except subprocess.TimeoutExpired:
            return False, "ffprobe timeout (file may be corrupted)"
        except Exception as e:
            return False, f"Validation error: {str(e)[:100]}"
    
    def _get_client(self):
        """Lazy load OpenAI client with timeout for Whisper API"""
        if self._client is None:
            if not self.api_key:
                return None  # Return None instead of raising, so we can fallback
            from openai import OpenAI
            import httpx
            # 5 minute timeout for large audio transcriptions, 30s connect timeout
            self._client = OpenAI(
                api_key=self.api_key,
                timeout=httpx.Timeout(300.0, connect=30.0)
            )
        return self._client
    
    def _get_mlx_whisper(self):
        """Lazy load Lightning Whisper MLX for Apple Silicon GPU acceleration"""
        if self._mlx_model is None:
            # Only available on Apple Silicon
            if platform.system() != "Darwin" or platform.processor() != "arm":
                return None
            
            with self._model_lock:
                if self._mlx_model is None:  # Double-check after acquiring lock
                    try:
                        from lightning_whisper_mlx import LightningWhisperMLX
                        print("  📥 Loading Lightning Whisper MLX (Apple Silicon GPU)...")
                        # Use distil-medium.en for good speed/quality balance
                        self._mlx_model = LightningWhisperMLX(model="distil-medium.en", batch_size=12, quant=None)
                        print("  ✓ Lightning Whisper MLX loaded")
                    except ImportError:
                        return None
                    except Exception as e:
                        print(f"  ⚠ Failed to load Lightning Whisper MLX: {e}")
                        return None
        return self._mlx_model
    
    def _get_local_whisper(self):
        """Lazy load faster-whisper model with GPU acceleration if available"""
        if self._local_model is None:
            try:
                from faster_whisper import WhisperModel
                
                # Detect best available device
                device = "cpu"
                compute_type = "int8"
                
                if platform.system() == "Darwin" and platform.processor() == "arm":
                    # Apple Silicon - use CPU with int8 (MPS not yet supported by faster-whisper)
                    device = "cpu"
                    compute_type = "int8"
                else:
                    try:
                        import torch
                        if torch.cuda.is_available():
                            device = "cuda"
                            compute_type = "float16"
                    except ImportError:
                        pass
                
                # Use "tiny" model for faster processing (3-4x faster than "base")
                # Quality is still good for most transcription tasks
                print(f"  📥 Loading faster-whisper model (device={device}, compute={compute_type})...")
                self._local_model = WhisperModel("tiny", device=device, compute_type=compute_type)
                print("  ✓ faster-whisper model loaded")
                
            except ImportError:
                print("  ⚠ faster-whisper not installed. Run: pip install faster-whisper")
                return None
            except Exception as e:
                print(f"  ⚠ Failed to load faster-whisper: {e}")
                return None
        return self._local_model
    
    def _load_index(self) -> Dict[str, Any]:
        """Load existing extraction index"""
        if self.index_file.exists():
            with open(self.index_file, 'r') as f:
                return json.load(f)
        return {"files": {}, "stats": {"total": 0, "processed": 0, "failed": 0}}
    
    def _save_index(self):
        """Save extraction index"""
        with open(self.index_file, 'w') as f:
            json.dump(self.index, f, indent=2)
    
    def _file_hash(self, filepath: Path) -> str:
        """Generate hash for file to detect changes
        
        Uses relative path from base_path to ensure consistent hashes
        across different environments (local vs server).
        """
        relative_path = filepath.relative_to(self.base_path)
        return hashlib.md5(f"{relative_path}_{filepath.stat().st_size}".encode()).hexdigest()
    
    def _clean_filename(self, filename: str) -> str:
        """Decode URL-encoded filenames and clean them"""
        from urllib.parse import unquote
        return unquote(filename)
    
    def _get_efta_dataset(self, filename: str, filepath: Path = None) -> str:
        """Determine which dataset an EFTA file belongs to.
        
        Uses folder-based detection first (most reliable), then falls back
        to EFTA number ranges.
        
        Data Set ranges (by EFTA number):
        - Data Set 1: 1-3158
        - Data Set 2: 3159-3857
        - Data Set 3: 3858-5704
        - Data Set 4: 5705-8408
        - Data Set 5: 8409-8528
        - Data Set 6: 8529-9015
        - Data Set 7: 9016-9675
        - Data Set 8: 9676-39024
        - Data Set 9: 39025-1262781 (released January 2026)
        - Data Set 10: 1262782-2212882 (released January 2026)
        - Data Set 11: 2212883-2730264 (released January 2026)
        - Data Set 12: 2730265+ (released January 2026)
        """
        import re
        
        # First, try folder-based detection (works for all data sets)
        if filepath is not None:
            try:
                path_str = str(filepath)
                folder_match = re.search(r'Data Set (\d+)', path_str)
                if folder_match:
                    return f"Data Set {folder_match.group(1)}"
            except:
                pass
        
        # Fall back to EFTA number-based detection
        match = re.search(r'EFTA(\d+)', filename)
        if not match:
            return "EFTA Documents"
        
        file_num = int(match.group(1))
        
        # Dataset ranges per DOJ website
        if file_num <= 3158:
            return "Data Set 1"
        elif file_num <= 3857:
            return "Data Set 2"
        elif file_num <= 5704:
            return "Data Set 3"
        elif file_num <= 8408:
            return "Data Set 4"
        elif file_num <= 8528:
            return "Data Set 5"
        elif file_num <= 9015:
            return "Data Set 6"
        elif file_num <= 9675:
            return "Data Set 7"
        elif file_num < 39025:
            return "Data Set 8"
        elif file_num < 1262782:
            return "Data Set 9"
        elif file_num < 2212883:
            return "Data Set 10"
        elif file_num < 2730265:
            return "Data Set 11"
        else:
            return "Data Set 12"
    
    def _categorize_file(self, filepath: Path) -> Dict[str, str]:
        """Categorize file based on path and filename"""
        relative_path = filepath.relative_to(self.base_path)
        parts = relative_path.parts
        filename = self._clean_filename(filepath.name)
        
        # Check extension for media type
        ext = filepath.suffix.lower()
        if ext in AUDIO_EXTENSIONS:
            media_type = "Audio Recording"
        elif ext in VIDEO_EXTENSIONS:
            media_type = "Video Recording"
        else:
            media_type = "Media File"
        
        category = "Unknown"
        subcategory = media_type
        
        # EFTA files always go to DOJ Disclosures regardless of folder location
        if filename.startswith("EFTA"):
            category = "DOJ Disclosures"
            subcategory = self._get_efta_dataset(filename, filepath)
            return {"category": category, "subcategory": subcategory}
        
        # Categorize based on folder structure
        if "DOJ Disclosures" in parts:
            category = "DOJ Disclosures"
            # Determine specific subcategory for DOJ media files
            if "Day 1" in filename or "Day 2" in filename or "Tallahassee" in filename:
                subcategory = "Maxwell Proffer"
            elif "2019.08" in filename:
                subcategory = "BOP Video Footage"
            else:
                subcategory = media_type
        elif "FBI Vault" in parts:
            category = "FBI Vault"
            # Extract part number from filename
            import re
            match = re.search(r'Part (\d+) of (\d+)', filename)
            if match:
                part_num = int(match.group(1))
                total_parts = int(match.group(2))
                subcategory = f"Part {part_num:02d} of {total_parts:02d}"
            else:
                subcategory = "FBI Records"
        elif "House Disclosures" in parts:
            category = "House Disclosures"
            # Subcategorize by production folder
            subcategory = self._get_house_disclosures_subcategory(relative_path)
        elif "CourtRecords" in parts:
            category = "Court Records"
            # Get case name from subfolder
            subcategory = self._get_court_case_name(relative_path)
        elif "FOIA" in parts:
            category = "FOIA"
            # Get agency/source from subfolder
            subcategory = self._get_foia_subcategory(relative_path)
        elif "Recordings" in parts or "Audio" in parts:
            category = "Recordings"
        elif "Video" in parts:
            category = "Video Evidence"
        elif "Depositions" in parts:
            category = "Depositions"
        else:
            # Use parent folder as category, but skip ignored directories
            if len(parts) > 1 and parts[0] not in IGNORED_DIRS:
                category = parts[0]
        
        return {"category": category, "subcategory": subcategory}
    
    def _get_court_case_name(self, relative_path: Path) -> str:
        """Extract court case name from path for subcategorization"""
        parts = relative_path.parts
        try:
            court_idx = parts.index("CourtRecords")
            if court_idx + 1 < len(parts) - 1:
                return parts[court_idx + 1]
        except (ValueError, IndexError):
            pass
        return "Legal_Filings"
    
    def _get_house_disclosures_subcategory(self, relative_path: Path) -> str:
        """Extract House Disclosures subcategory from path (production folder)
        
        Structure: House Disclosures / Prod 01_20250822 / VOL00001 / NATIVES / NATIVE008 / file.mp4
        Subcategory should be the production folder (e.g., "Prod 01_20250822")
        """
        parts = relative_path.parts
        
        # Find the House Disclosures folder and get the next subfolder (production)
        try:
            hd_idx = parts.index("House Disclosures")
            if hd_idx + 1 < len(parts):
                folder_name = parts[hd_idx + 1]
                # Only use folder name if it's not the file itself
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4', '.jpg', '.tif', '.dat', '.opt')):
                    return folder_name
        except (ValueError, IndexError):
            pass
        
        return "Other Documents"
    
    def _get_foia_subcategory(self, relative_path: Path) -> str:
        """Extract FOIA subcategory from path (agency/source folder)"""
        parts = relative_path.parts
        try:
            foia_idx = parts.index("FOIA")
            if foia_idx + 1 < len(parts):
                folder_name = parts[foia_idx + 1]
                if not folder_name.endswith(('.pdf', '.wav', '.mp3', '.mp4')):
                    return folder_name
        except (ValueError, IndexError):
            pass
        
        # Fallback: try to determine source from filename patterns
        filename = self._clean_filename(relative_path.name)
        
        if "BOP" in filename or "Bureau of Prisons" in filename:
            return "Federal Bureau of Prisons (BOP)"
        elif "TECS" in filename or "Travel" in filename or "Aircraft" in filename:
            return "Customs and Border Protection (CBP)"
        elif filename.startswith("Epstein Part") and "Redacted" not in filename:
            return "Federal Bureau of Investigation (FBI)"
        elif filename.startswith("Epstein Travel"):
            return "Travel Records"
        elif "06CF009454" in filename or "Redacted" in filename or "Stac" in filename or "Phone" in filename:
            return "Florida"
        
        return "Other FOIA Documents"
    
    def _get_file_duration(self, filepath: Path) -> Optional[float]:
        """Get duration of audio/video file in seconds (if ffprobe available)"""
        try:
            import subprocess
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(filepath)],
                capture_output=True, text=True
            )
            return float(result.stdout.strip())
        except:
            return None
    
    def _transcribe_with_openai(self, filepath: Path) -> Optional[str]:
        """Transcribe using OpenAI Whisper API"""
        client = self._get_client()
        if not client:
            return None
            
        file_size = filepath.stat().st_size
        max_size = 25 * 1024 * 1024  # 25MB
        
        if file_size > max_size:
            print(f"    File too large for OpenAI ({file_size / 1024 / 1024:.1f}MB), using local...")
            return None
        
        try:
            with open(filepath, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="verbose_json"
                )
            return transcript.text
        except Exception as e:
            print(f"    OpenAI transcription failed: {e}")
            return None
    
    def _transcribe_with_mlx(self, filepath: Path) -> Optional[str]:
        """Transcribe using Lightning Whisper MLX (Apple Silicon GPU accelerated)"""
        model = self._get_mlx_whisper()
        if not model:
            return None
        
        try:
            result = model.transcribe(audio_path=str(filepath))
            text = result.get('text', '').strip() if isinstance(result, dict) else str(result).strip()
            return text if text else "[No speech detected]"
        except Exception as e:
            print(f"    MLX transcription failed: {e}")
            return None
    
    def _transcribe_with_local(self, filepath: Path) -> Optional[str]:
        """Transcribe using faster-whisper model"""
        model = self._get_local_whisper()
        if not model:
            return None
        
        try:
            # faster-whisper API: returns segments iterator and info
            # beam_size=1 is faster, vad_filter=True skips silence
            segments, info = model.transcribe(str(filepath), beam_size=1, vad_filter=True)
            # Collect all segment texts
            text_parts = [segment.text for segment in segments]
            result = " ".join(text_parts).strip()
            # Return empty string for silent/empty audio (not None, so we don't retry)
            return result if result else "[No speech detected]"
        except IndexError:
            # "tuple index out of range" = empty/silent audio
            return "[No speech detected]"
        except Exception as e:
            print(f"    Local transcription failed: {e}")
            return None
    
    def _convert_to_wav(self, filepath: Path) -> Optional[Path]:
        """Convert audio/video to WAV format for better compatibility"""
        try:
            import subprocess
            output_path = self.extracted_dir / f"temp_{filepath.stem}.wav"
            
            # Use ffmpeg to convert to WAV
            result = subprocess.run([
                'ffmpeg', '-y', '-i', str(filepath),
                '-ar', '16000',  # 16kHz sample rate (Whisper optimal)
                '-ac', '1',      # Mono
                '-f', 'wav',
                str(output_path)
            ], capture_output=True, text=True)
            
            if result.returncode == 0 and output_path.exists():
                return output_path
            return None
        except Exception as e:
            print(f"    Conversion failed: {e}")
            return None
    
    def _extract_audio_from_video(self, filepath: Path) -> Optional[Path]:
        """Extract audio track from video file for faster processing"""
        if filepath.suffix.lower() not in VIDEO_EXTENSIONS:
            return None  # Not a video file
        
        try:
            output_path = self.extracted_dir / f"temp_audio_{filepath.stem}.mp3"
            
            result = subprocess.run([
                'ffmpeg', '-y', '-i', str(filepath),
                '-vn',           # No video
                '-acodec', 'libmp3lame',
                '-ar', '16000',  # 16kHz (Whisper optimal)
                '-ac', '1',      # Mono
                '-q:a', '4',     # Quality level
                str(output_path)
            ], capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0 and output_path.exists():
                return output_path
            return None
        except Exception as e:
            print(f"    Audio extraction failed: {e}")
            return None
    
    def _chunk_audio(self, filepath: Path, chunk_duration: int = 600) -> List[Path]:
        """Split audio into chunks for parallel processing (default 10 min chunks)"""
        try:
            duration = self._get_file_duration(filepath)
            
            if duration is None or duration <= chunk_duration:
                return [filepath]  # No need to chunk
            
            chunks = []
            for i in range(0, int(duration), chunk_duration):
                output_path = self.extracted_dir / f"chunk_{filepath.stem}_{i}.wav"
                result = subprocess.run([
                    'ffmpeg', '-y', '-i', str(filepath),
                    '-ss', str(i),
                    '-t', str(chunk_duration),
                    '-ar', '16000',
                    '-ac', '1',
                    '-f', 'wav',
                    str(output_path)
                ], capture_output=True, text=True, timeout=120)
                
                if result.returncode == 0 and output_path.exists():
                    chunks.append(output_path)
            
            return chunks if chunks else [filepath]
        except Exception as e:
            print(f"    Chunking failed: {e}")
            return [filepath]
    
    def transcribe_file(self, filepath: Path) -> Optional[Dict[str, Any]]:
        """Transcribe audio/video file with optimized processing"""
        try:
            file_size = filepath.stat().st_size
            
            # Quick validation first (< 2 seconds) to skip corrupted files fast
            is_valid, validation_error = self._validate_media_file(filepath)
            if not is_valid:
                self._save_failed_file(filepath, validation_error)
                return {
                    "id": self._file_hash(filepath),
                    "filename": self._clean_filename(filepath.name),
                    "path": str(filepath.relative_to(self.base_path)),
                    "error": f"Invalid media: {validation_error}",
                    "has_content": False
                }
            
            # For video files, extract audio first (much faster to process)
            audio_path = filepath
            extracted_audio = None
            if filepath.suffix.lower() in VIDEO_EXTENSIONS:
                extracted_audio = self._extract_audio_from_video(filepath)
                if extracted_audio:
                    audio_path = extracted_audio
                else:
                    # Audio extraction failed - log and skip
                    self._save_failed_file(filepath, "Audio extraction failed")
                    return {
                        "id": self._file_hash(filepath),
                        "filename": self._clean_filename(filepath.name),
                        "path": str(filepath.relative_to(self.base_path)),
                        "error": "Audio extraction failed",
                        "has_content": False
                    }
            
            full_text = None
            transcription_method = "unknown"
            
            # On Apple Silicon, try Lightning Whisper MLX first (GPU accelerated, fastest)
            if platform.system() == "Darwin" and platform.processor() == "arm":
                full_text = self._transcribe_with_mlx(audio_path)
                if full_text:
                    transcription_method = "mlx"
            
            # Fallback to faster-whisper (CPU)
            if not full_text:
                full_text = self._transcribe_with_local(audio_path)
                if full_text:
                    transcription_method = "faster-whisper"
            
            # If direct transcription fails, try converting to WAV first
            if not full_text:
                wav_path = self._convert_to_wav(audio_path)
                if wav_path:
                    full_text = self._transcribe_with_local(wav_path)
                    if full_text:
                        transcription_method = "faster-whisper"
                    # Clean up temp file
                    try:
                        wav_path.unlink()
                    except:
                        pass
            
            # Fallback to OpenAI Whisper API if local fails (25MB limit)
            if not full_text and self.api_key and audio_path.stat().st_size < 25 * 1024 * 1024:
                full_text = self._transcribe_with_openai(audio_path)
                if full_text:
                    transcription_method = "openai"
            
            # Cleanup extracted audio
            if extracted_audio and extracted_audio.exists():
                try:
                    extracted_audio.unlink()
                except:
                    pass
            
            # If still no transcription, log and return error
            if not full_text:
                self._save_failed_file(filepath, "All transcription methods failed")
                return {
                    "id": self._file_hash(filepath),
                    "filename": self._clean_filename(filepath.name),
                    "path": str(filepath.relative_to(self.base_path)),
                    "error": "All transcription methods failed",
                    "has_content": False
                }
            
            # Build result
            relative_path = filepath.relative_to(self.base_path)
            clean_name = self._clean_filename(filepath.name)
            categorization = self._categorize_file(filepath)
            duration = self._get_file_duration(filepath)
            media_type = "audio" if filepath.suffix.lower() in AUDIO_EXTENSIONS else "video"
            
            return {
                "id": self._file_hash(filepath),
                "filename": clean_name,
                "original_filename": filepath.name,
                "path": str(relative_path),
                "full_path": str(filepath),
                "category": categorization["category"],
                "subcategory": categorization["subcategory"],
                "file_type": media_type,
                "media_type": media_type,
                "duration_seconds": duration,
                "transcription_method": transcription_method,
                "full_text": full_text,
                "char_count": len(full_text),
                "has_content": len(full_text) > 0
            }
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {
                "id": self._file_hash(filepath),
                "filename": self._clean_filename(filepath.name),
                "path": str(filepath.relative_to(self.base_path)),
                "error": str(e),
                "has_content": False
            }
    
    def find_all_media(self) -> Generator[Path, None, None]:
        """Find all audio/video files in the base path, excluding ignored directories"""
        for root, dirs, files in os.walk(self.base_path):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            
            for file in files:
                ext = Path(file).suffix.lower()
                if ext in MEDIA_EXTENSIONS:
                    yield Path(root) / file
    
    def _process_result(self, result: Optional[Dict], results: Dict):
        """Helper to process transcription result and update index"""
        if result and result.get("has_content"):
            # Save transcription
            output_file = self.extracted_dir / f"{result['id']}.json"
            with open(output_file, 'w') as f:
                json.dump(result, f)
            
            # Thread-safe index update and save for resume support
            with self._index_lock:
                self.index["files"][result["id"]] = {
                    "filename": result["filename"],
                    "path": result["path"],
                    "category": result.get("category", "Unknown"),
                    "subcategory": result.get("subcategory", ""),
                    "media_type": result.get("media_type", "audio"),
                    "duration_seconds": result.get("duration_seconds"),
                    "char_count": result.get("char_count", 0)
                }
                self._save_index()
            results["success"] += 1
        else:
            error = result.get("error", "Unknown error") if result else "No result"
            print(f"  ✗ Failed: {error}")
            results["failed"] += 1
    
    def extract_all(self, force: bool = False, max_workers: int = 8, progress_callback=None) -> Dict[str, Any]:
        """Transcribe all audio/video files with parallel processing support
        
        Args:
            force: Force re-transcription of all files
            max_workers: Number of parallel workers
            progress_callback: Optional callback(current, total) for progress updates
        """
        media_files = list(self.find_all_media())
        
        if not media_files:
            print("No audio/video files found")
            return {"success": 0, "failed": 0, "skipped": 0}
        
        print(f"Found {len(media_files)} audio/video files")
        
        # Note: We can now use local faster-whisper even without API key
        if not self.api_key:
            print("  ℹ OPENAI_API_KEY not set - using local faster-whisper only")
        
        results = {"success": 0, "failed": 0, "skipped": 0}
        
        # Filter already processed files unless force
        to_process = []
        for media in media_files:
            file_hash = self._file_hash(media)
            if force or file_hash not in self.index["files"]:
                to_process.append(media)
            else:
                results["skipped"] += 1
        
        print(f"Processing {len(to_process)} new files ({results['skipped']} already processed)")
        print(f"  💡 Press Ctrl+\\ to skip current file, Ctrl+C to stop")
        
        # Setup signal handler for skip
        _setup_skip_signal()
        
        if not to_process:
            return results
        
        # On Apple Silicon, use sequential processing (MLX uses GPU, can't parallelize)
        # On other platforms, use parallel processing with faster-whisper
        use_mlx = platform.system() == "Darwin" and platform.processor() == "arm"
        
        # Timeout per file (5 minutes max)
        file_timeout = 300
        processed_count = 0
        
        if use_mlx:
            # Pre-load MLX model to catch errors early
            mlx_model = self._get_mlx_whisper()
            if mlx_model:
                print(f"  ⚡ Using Lightning Whisper MLX (Apple Silicon GPU)")
            else:
                print(f"  ℹ MLX not available, using faster-whisper (CPU)")
            
            # Sequential for MLX (GPU already provides parallelism)
            for media in tqdm(to_process, desc="Transcribing"):
                _reset_skip_flag()
                processed_count += 1
                
                # Call progress callback if provided
                if progress_callback:
                    try:
                        progress_callback(processed_count, len(to_process))
                    except:
                        pass
                
                try:
                    # Run transcription with timeout using a thread
                    result_container = [None]
                    error_container = [None]
                    
                    def transcribe_with_timeout():
                        try:
                            result_container[0] = self.transcribe_file(media)
                        except Exception as e:
                            error_container[0] = e
                    
                    thread = threading.Thread(target=transcribe_with_timeout)
                    thread.start()
                    
                    # Wait with periodic skip checks
                    start_time = time.time()
                    while thread.is_alive():
                        thread.join(timeout=0.5)  # Check every 0.5s
                        if _should_skip():
                            print(f"\n  ⏭ Skipping: {media.name}")
                            self._save_failed_file(media, "Skipped by user")
                            results["failed"] += 1
                            break
                        if time.time() - start_time > file_timeout:
                            print(f"\n  ⏱ Timeout ({file_timeout}s): {media.name}")
                            self._save_failed_file(media, f"Timeout after {file_timeout}s")
                            results["failed"] += 1
                            break
                    else:
                        # Thread completed normally
                        if error_container[0]:
                            raise error_container[0]
                        self._process_result(result_container[0], results)
                        
                except KeyboardInterrupt:
                    print(f"\n  ⚠ Interrupted - saving progress...")
                    break
                except Exception as e:
                    print(f"  ✗ Error: {media.name}: {e}")
                    self._save_failed_file(media, str(e))
                    results["failed"] += 1
        else:
            # Parallel processing for CPU-based transcription
            print(f"  🚀 Using parallel processing with {max_workers} workers")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self.transcribe_file, m): m for m in to_process}
                for future in tqdm(as_completed(futures), total=len(to_process), desc="Transcribing"):
                    media = futures[future]
                    processed_count += 1
                    
                    # Call progress callback if provided
                    if progress_callback:
                        try:
                            progress_callback(processed_count, len(to_process))
                        except:
                            pass
                    
                    try:
                        result = future.result(timeout=file_timeout)
                        self._process_result(result, results)
                    except FuturesTimeoutError:
                        print(f"  ⏱ Timeout ({file_timeout}s): {media.name}")
                        self._save_failed_file(media, f"Timeout after {file_timeout}s")
                        results["failed"] += 1
                    except Exception as e:
                        print(f"  ✗ Error: {media.name}: {e}")
                        results["failed"] += 1
        
        # Update stats
        self.index["stats"]["total"] = len(media_files)
        self.index["stats"]["processed"] = results["success"] + results["skipped"]
        self.index["stats"]["failed"] = results["failed"]
        self._save_index()
        
        # Print summary of failed files
        if self.failed_files["count"] > 0:
            print(f"\n  ⚠ {self.failed_files['count']} files failed - see: {self.failed_files_log}")
        
        return results


class MediaExtractor:
    """Combined extractor for PDFs, images, and audio/video files"""
    
    def __init__(self, base_path: str, api_key: str = None):
        self.base_path = Path(base_path)
        self.pdf_extractor = PDFExtractor(base_path)
        self.image_extractor = ImageExtractor(base_path)
        self.av_extractor = AudioVideoExtractor(base_path, api_key)
    
    def extract_all(self, max_workers: int = 4, force: bool = False,
                    pdf_progress_callback=None, image_progress_callback=None, 
                    media_progress_callback=None) -> Dict[str, Any]:
        """Extract/transcribe all supported files
        
        Args:
            max_workers: Number of parallel workers
            force: Force re-extraction of all files
            pdf_progress_callback: Optional callback(current, total) for PDF progress
            image_progress_callback: Optional callback(current, total) for image OCR progress
            media_progress_callback: Optional callback(current, total) for media transcription progress
        """
        print("\n📄 Processing PDF files...")
        pdf_results = self.pdf_extractor.extract_all(
            max_workers=max_workers, force=force, progress_callback=pdf_progress_callback
        )
        
        print("\n🖼️  Processing image files (OCR)...")
        image_results = self.image_extractor.extract_all(
            max_workers=max_workers, force=force, progress_callback=image_progress_callback
        )
        
        print("\n🎤 Processing audio/video files...")
        av_results = self.av_extractor.extract_all(
            force=force, max_workers=max_workers, progress_callback=media_progress_callback
        )
        
        # Combine results
        return {
            "pdf": pdf_results,
            "image": image_results,
            "media": av_results,
            "total_success": pdf_results["success"] + image_results["success"] + av_results["success"],
            "total_failed": pdf_results["failed"] + image_results["failed"] + av_results["failed"],
            "total_skipped": pdf_results["skipped"] + image_results["skipped"] + av_results["skipped"]
        }


if __name__ == "__main__":
    import sys
    
    base_path = sys.argv[1] if len(sys.argv) > 1 else "/Users/user/Documents/Epstein"
    extractor = MediaExtractor(base_path)
    
    print("Starting media extraction...")
    results = extractor.extract_all(max_workers=8)
    print(f"\nExtraction complete!")
    print(f"  PDFs   - Success: {results['pdf']['success']}, Failed: {results['pdf']['failed']}, Skipped: {results['pdf']['skipped']}")
    print(f"  Images - Success: {results['image']['success']}, Failed: {results['image']['failed']}, Skipped: {results['image']['skipped']}")
    print(f"  Media  - Success: {results['media']['success']}, Failed: {results['media']['failed']}, Skipped: {results['media']['skipped']}")


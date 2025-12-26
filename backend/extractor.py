"""
Media Extraction Module
Extracts text from PDFs and transcribes audio/video files for indexing
"""

import os
import json
import hashlib
from pathlib import Path
from typing import Generator, Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import pdfplumber

# Supported file types
PDF_EXTENSIONS = {'.pdf'}
AUDIO_EXTENSIONS = {'.wav', '.mp3', '.m4a', '.ogg', '.flac', '.aac', '.wma'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv'}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# Directories to ignore during extraction
IGNORED_DIRS = {
    'venv', '.venv', 'env', '.env',  # Python virtual environments
    'node_modules',                   # Node.js
    '.git', '.svn', '.hg',           # Version control
    '__pycache__', '.pytest_cache',   # Python cache
    'extracted_text', 'transcripts',  # Our output directories
    '.cursor', '.vscode', '.idea',    # IDE directories
}


class PDFExtractor:
    """Extracts text content from PDF files"""
    
    def __init__(self, base_path: str):
        self.base_path = Path(base_path)
        self.extracted_dir = self.base_path / "extracted_text"
        self.extracted_dir.mkdir(exist_ok=True)
        self.index_file = self.extracted_dir / "index.json"
        self.index = self._load_index()
    
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
        """Generate hash for file to detect changes"""
        return hashlib.md5(f"{filepath}_{filepath.stat().st_size}".encode()).hexdigest()
    
    def _clean_filename(self, filename: str) -> str:
        """Decode URL-encoded filenames and clean them"""
        from urllib.parse import unquote
        return unquote(filename)
    
    def _get_efta_dataset(self, filename: str) -> str:
        """Determine which dataset an EFTA file belongs to based on file number"""
        import re
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
        else:
            return "Data Set 8"
    
    def _categorize_file(self, filepath: Path) -> Dict[str, str]:
        """Categorize file based on path and filename"""
        relative_path = filepath.relative_to(self.base_path)
        parts = relative_path.parts
        
        category = "Unknown"
        subcategory = ""
        
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
        elif "CourtRecords" in parts:
            category = "Court Records"
            # Get subcategory from subfolder name (case name)
            subcategory = self._get_court_case_name(relative_path)
        elif "FOIA" in parts:
            category = "FOIA"
            # Get subcategory from subfolder name (agency name)
            subcategory = self._get_foia_subcategory(relative_path)
        
        return {"category": category, "subcategory": subcategory}
    
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
        elif "TECS" in filename or "Travel" in filename or "Aircraft" in filename or "Records" in filename:
            return "Customs and Border Protection (CBP)"
        elif filename.startswith("Epstein Part") and "Redacted" not in filename:
            return "Federal Bureau of Investigation (FBI)"
        elif "06CF009454" in filename or "Redacted" in filename or "Stac" in filename or "Phone" in filename:
            return "Florida"
        
        return "Other"
    
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
    
    def extract_pdf(self, filepath: Path) -> Optional[Dict[str, Any]]:
        """Extract text from a single PDF file"""
        try:
            pages = []
            full_text = []
            
            with pdfplumber.open(str(filepath)) as pdf:
                for page_num, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
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
            
            return {
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
    
    def extract_all(self, max_workers: int = 4, force: bool = False) -> Dict[str, Any]:
        """Extract text from all PDFs"""
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
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.extract_pdf, pdf): pdf for pdf in to_process}
            
            for future in tqdm(as_completed(futures), total=len(to_process), desc="Extracting"):
                pdf = futures[future]
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
                            "page_count": result.get("page_count", 0),
                            "char_count": result.get("char_count", 0)
                        }
                        results["success"] += 1
                    else:
                        results["failed"] += 1
                except Exception as e:
                    print(f"Error processing {pdf}: {e}")
                    results["failed"] += 1
        
        # Update stats
        self.index["stats"]["total"] = len(pdfs)
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
        self.index = self._load_index()
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = None
        self._local_model = None
        self._use_local_whisper = False  # Track if we should use local
    
    def _get_client(self):
        """Lazy load OpenAI client"""
        if self._client is None:
            if not self.api_key:
                return None  # Return None instead of raising, so we can fallback
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key)
        return self._client
    
    def _get_local_whisper(self):
        """Lazy load local Whisper model"""
        if self._local_model is None:
            try:
                import whisper
                print("  📥 Loading local Whisper model (first time may take a while)...")
                self._local_model = whisper.load_model("base")  # Use "base" for balance of speed/accuracy
                print("  ✓ Local Whisper model loaded")
            except ImportError:
                print("  ⚠ Local Whisper not installed. Run: pip install openai-whisper")
                return None
            except Exception as e:
                print(f"  ⚠ Failed to load local Whisper: {e}")
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
        """Generate hash for file to detect changes"""
        return hashlib.md5(f"{filepath}_{filepath.stat().st_size}".encode()).hexdigest()
    
    def _clean_filename(self, filename: str) -> str:
        """Decode URL-encoded filenames and clean them"""
        from urllib.parse import unquote
        return unquote(filename)
    
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
            # Use parent folder as category
            if len(parts) > 1:
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
        
        # Fallback based on filename patterns
        filename = relative_path.name
        if "06CF009454" in filename:
            return "Florida"
        return "Other"
    
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
    
    def _transcribe_with_local(self, filepath: Path) -> Optional[str]:
        """Transcribe using local Whisper model"""
        model = self._get_local_whisper()
        if not model:
            return None
        
        try:
            # For large files, we may need to convert/process
            result = model.transcribe(str(filepath), fp16=False)
            return result.get("text", "")
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
    
    def transcribe_file(self, filepath: Path) -> Optional[Dict[str, Any]]:
        """Transcribe audio/video file with OpenAI fallback to local Whisper"""
        try:
            file_size = filepath.stat().st_size
            print(f"  🎤 Transcribing: {filepath.name} ({file_size / 1024 / 1024:.1f}MB)")
            
            full_text = None
            transcription_method = "unknown"
            
            # Try OpenAI first (faster, better quality, but has size limit)
            if not self._use_local_whisper and self.api_key:
                print(f"    Trying OpenAI Whisper...")
                full_text = self._transcribe_with_openai(filepath)
                if full_text:
                    transcription_method = "openai"
                    print(f"    ✓ OpenAI transcription successful ({len(full_text)} chars)")
            
            # Fallback to local Whisper
            if not full_text:
                print(f"    Trying local Whisper...")
                
                # Try direct transcription first
                full_text = self._transcribe_with_local(filepath)
                
                # If that fails, try converting to WAV first
                if not full_text:
                    print(f"    Converting to WAV and retrying...")
                    wav_path = self._convert_to_wav(filepath)
                    if wav_path:
                        full_text = self._transcribe_with_local(wav_path)
                        # Clean up temp file
                        try:
                            wav_path.unlink()
                        except:
                            pass
                
                if full_text:
                    transcription_method = "local"
                    print(f"    ✓ Local transcription successful ({len(full_text)} chars)")
            
            # If still no transcription, return error
            if not full_text:
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
    
    def extract_all(self, force: bool = False) -> Dict[str, Any]:
        """Transcribe all audio/video files"""
        media_files = list(self.find_all_media())
        
        if not media_files:
            print("No audio/video files found")
            return {"success": 0, "failed": 0, "skipped": 0}
        
        print(f"Found {len(media_files)} audio/video files")
        
        # Check for API key
        if not self.api_key:
            print("⚠ OPENAI_API_KEY not set - cannot transcribe audio/video files")
            return {"success": 0, "failed": 0, "skipped": len(media_files)}
        
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
        
        # Process sequentially (API rate limits)
        for media in tqdm(to_process, desc="Transcribing"):
            try:
                result = self.transcribe_file(media)
                if result and result.get("has_content"):
                    # Save transcription
                    output_file = self.extracted_dir / f"{result['id']}.json"
                    with open(output_file, 'w') as f:
                        json.dump(result, f)
                    
                    self.index["files"][result["id"]] = {
                        "filename": result["filename"],
                        "path": result["path"],
                        "category": result.get("category", "Unknown"),
                        "subcategory": result.get("subcategory", ""),
                        "media_type": result.get("media_type", "audio"),
                        "duration_seconds": result.get("duration_seconds"),
                        "char_count": result.get("char_count", 0)
                    }
                    results["success"] += 1
                else:
                    error = result.get("error", "Unknown error") if result else "No result"
                    print(f"  ✗ Failed: {media.name} - {error}")
                    results["failed"] += 1
            except Exception as e:
                print(f"  ✗ Error processing {media.name}: {e}")
                results["failed"] += 1
        
        # Update stats
        self.index["stats"]["total"] = len(media_files)
        self.index["stats"]["processed"] = results["success"] + results["skipped"]
        self.index["stats"]["failed"] = results["failed"]
        self._save_index()
        
        return results


class MediaExtractor:
    """Combined extractor for PDFs and audio/video files"""
    
    def __init__(self, base_path: str, api_key: str = None):
        self.base_path = Path(base_path)
        self.pdf_extractor = PDFExtractor(base_path)
        self.av_extractor = AudioVideoExtractor(base_path, api_key)
    
    def extract_all(self, max_workers: int = 4, force: bool = False) -> Dict[str, Any]:
        """Extract/transcribe all supported files"""
        print("\n📄 Processing PDF files...")
        pdf_results = self.pdf_extractor.extract_all(max_workers=max_workers, force=force)
        
        print("\n🎤 Processing audio/video files...")
        av_results = self.av_extractor.extract_all(force=force)
        
        # Combine results
        return {
            "pdf": pdf_results,
            "media": av_results,
            "total_success": pdf_results["success"] + av_results["success"],
            "total_failed": pdf_results["failed"] + av_results["failed"],
            "total_skipped": pdf_results["skipped"] + av_results["skipped"]
        }


if __name__ == "__main__":
    import sys
    
    base_path = sys.argv[1] if len(sys.argv) > 1 else "/Users/user/Documents/Epstein"
    extractor = MediaExtractor(base_path)
    
    print("Starting media extraction...")
    results = extractor.extract_all(max_workers=8)
    print(f"\nExtraction complete!")
    print(f"  PDFs - Success: {results['pdf']['success']}, Failed: {results['pdf']['failed']}, Skipped: {results['pdf']['skipped']}")
    print(f"  Media - Success: {results['media']['success']}, Failed: {results['media']['failed']}, Skipped: {results['media']['skipped']}")


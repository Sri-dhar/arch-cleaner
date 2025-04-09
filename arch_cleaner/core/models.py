import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Any, Dict

# --- Data Representation ---

@dataclass
class ScannedItem:
    """Base class for items found during scan."""
    path: Path
    size_bytes: int
    last_accessed: float = field(default_factory=time.time) # Defaults to now, should be updated by scanner
    last_modified: float = field(default_factory=time.time) # Defaults to now, should be updated by scanner
    item_type: str = "file" # e.g., file, directory, cache, log
    extra_info: Dict[str, Any] = field(default_factory=dict) # For type-specific data

@dataclass
class PackageInfo:
    """Represents information about an installed package."""
    name: str
    version: str
    size_bytes: int
    description: Optional[str] = None
    install_date: Optional[float] = None
    last_used: Optional[float] = None # Heuristic, might be hard to determine accurately
    is_orphan: bool = False
    is_dependency: bool = False
    required_by: List[str] = field(default_factory=list)
    optional_for: List[str] = field(default_factory=list)

@dataclass
class DuplicateSet:
    """Represents a set of duplicate files."""
    file_hash: str
    paths: List[Path]
    size_bytes: int # Size of a single file in the set
    total_size_bytes: int # Total size occupied by all duplicates (size * count)

# --- Suggestions & Actions ---

@dataclass
class Suggestion:
    """Represents a single cleanup suggestion."""
    id: str # Unique identifier (e.g., hash of details)
    suggestion_type: str # e.g., OLD_FILE, PACMAN_CACHE, ORPHAN_PACKAGE, DUPLICATE_SET, LARGE_DIR, JOURNAL_LOG
    description: str # User-friendly description of the suggestion
    details: str # More specific details (e.g., file path, package name)
    estimated_size_bytes: int # Estimated space saving
    confidence: float = 0.5 # Confidence score (0.0 to 1.0)
    rationale: str = "" # Reason why this is suggested
    data: Any = None # Reference to the underlying data (ScannedItem, PackageInfo, DuplicateSet, etc.)

@dataclass
class ActionFeedback:
    """Represents user feedback on a suggestion."""
    suggestion_id: str
    action_taken: str # e.g., APPROVED, REJECTED, SKIPPED
    timestamp: float = field(default_factory=time.time)
    user_comment: Optional[str] = None

@dataclass
class ActionResult:
    """Represents the result of an executed action."""
    suggestion: Suggestion
    success: bool
    message: str
    bytes_freed: int = 0
    dry_run: bool = False

# --- Configuration Models (Optional, can also be simple dicts) ---
# Example: Could define dataclasses for sections of the config for type safety
# @dataclass
# class ArchConfig:
#     clean_pacman_cache: bool
#     pacman_cache_keep: int
#     # ... and so on

# @dataclass
# class AppConfig:
#     scan_paths: List[str]
#     exclude_patterns: List[str]
#     arch: ArchConfig
#     # ... other sections

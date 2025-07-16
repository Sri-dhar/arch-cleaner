import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Any, Dict


@dataclass
class ScannedItem:
    """Represents a single scanned item, like a file or directory."""
    path: Path
    size_bytes: int
    last_accessed: float = field(default_factory=time.time)
    last_modified: float = field(default_factory=time.time)
    item_type: str = "file"
    extra_info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PackageInfo:
    """Represents information about an installed package."""
    name: str
    version: str
    size_bytes: int
    description: Optional[str] = None
    install_date: Optional[float] = None
    last_used: Optional[float] = None
    is_orphan: bool = False
    is_dependency: bool = False
    required_by: List[str] = field(default_factory=list)
    optional_for: List[str] = field(default_factory=list)


@dataclass
class DuplicateSet:
    """Represents a set of duplicate files identified by their hash."""
    file_hash: str
    paths: List[Path]
    size_bytes: int
    total_size_bytes: int


@dataclass
class Suggestion:
    """Represents a single cleanup suggestion for the user."""
    id: str
    suggestion_type: str
    description: str
    details: str
    estimated_size_bytes: int
    confidence: float = 0.5
    rationale: str = ""
    data: Any = None


@dataclass
class ActionFeedback:
    """Records user feedback on a given suggestion."""
    suggestion_id: str
    action_taken: str
    timestamp: float = field(default_factory=time.time)
    user_comment: Optional[str] = None


@dataclass
class ActionResult:
    """Summarizes the result of an executed action."""
    suggestion: Suggestion
    success: bool
    message: str
    bytes_freed: int = 0
    dry_run: bool = False

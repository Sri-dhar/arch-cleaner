import logging
import hashlib
import time
import re
from pathlib import Path
from typing import List, Dict, Any, Iterator, Tuple, Optional # Added Optional

# Project imports
from ..core.models import ScannedItem, PackageInfo, DuplicateSet, Suggestion
from ..modules.config_manager import ConfigManager
from ..utils.helpers import human_readable_size, get_age_seconds, parse_duration, parse_size
from .collection import ITEM_TYPE_PACMAN_CACHE, ITEM_TYPE_JOURNAL_LOG # Import constants

logger = logging.getLogger(__name__)

# Suggestion Type Constants
SUGGESTION_OLD_FILE = "OLD_FILE"
SUGGESTION_LARGE_FILE = "LARGE_FILE"
SUGGESTION_ORPHAN_PACKAGE = "ORPHAN_PACKAGE"
SUGGESTION_DUPLICATE_SET = "DUPLICATE_SET"
SUGGESTION_PACMAN_CACHE = "PACMAN_CACHE"
SUGGESTION_JOURNAL_LOG = "JOURNAL_LOG"
# Add more types as needed

class RecommendationEngine:
    """Generates cleanup suggestions based on analysis results."""

    def __init__(self, config_manager: ConfigManager):
        self.config = config_manager
        # Load relevant config values for recommendation logic
        self.pacman_cache_keep = self.config.get('arch.pacman_cache_keep', 1)
        self.journal_max_size_str = self.config.get('arch.journal_max_disk_size')
        self.journal_max_age_str = self.config.get('arch.journal_max_age')
        self.journal_max_bytes = parse_size(self.journal_max_size_str) if self.journal_max_size_str else None
        self.journal_max_seconds = parse_duration(self.journal_max_age_str) if self.journal_max_age_str else None
        # Confidence thresholds or aggressiveness could be used here later

    def generate_suggestions(self, analysis_results: Dict[str, List[Any]]) -> List[Suggestion]:
        """
        Generates a list of Suggestion objects from the analysis results.

        Args:
            analysis_results: Dictionary containing lists of items from AnalysisEngine.

        Returns:
            A list of Suggestion objects, potentially sorted by potential savings.
        """
        logger.info("Generating recommendations from analysis results...")
        suggestions = []

        # Use iterators to generate suggestions for each category
        suggestion_generators = [
            self._generate_old_file_suggestions(analysis_results.get('old_files', [])),
            self._generate_large_file_suggestions(analysis_results.get('large_files', [])),
            self._generate_orphan_package_suggestions(analysis_results.get('orphan_packages', [])),
            self._generate_duplicate_set_suggestions(analysis_results.get('duplicate_sets', [])),
            self._generate_pacman_cache_suggestions(analysis_results.get('pacman_cache_files', [])),
            self._generate_journal_log_suggestions(analysis_results.get('journal_logs', [])),
        ]

        for generator in suggestion_generators:
            try:
                suggestions.extend(list(generator))
            except Exception as e:
                logger.error(f"Error generating suggestions for a category: {e}", exc_info=True)

        # Sort suggestions (e.g., by estimated size descending)
        suggestions.sort(key=lambda s: s.estimated_size_bytes, reverse=True)

        logger.info(f"Generated {len(suggestions)} recommendations.")
        return suggestions

    def _generate_suggestion_id(self, suggestion_type: str, details: str) -> str:
        """Creates a unique but deterministic ID for a suggestion."""
        # Use a hash of type and key details to make it reproducible
        hasher = hashlib.sha1()
        hasher.update(suggestion_type.encode())
        hasher.update(details.encode())
        return hasher.hexdigest()[:10] # Short hash for readability

    def _generate_old_file_suggestions(self, old_files: List[ScannedItem]) -> Iterator[Suggestion]:
        """Generates suggestions for old files."""
        for item in old_files:
            age_days = int(get_age_seconds(item.last_accessed) / 86400)
            size_hr = human_readable_size(item.size_bytes)
            details = str(item.path)
            desc = f"Remove old file not accessed in {age_days} days ({size_hr})"
            rationale = f"File last accessed on {time.ctime(item.last_accessed)}."
            # Simple confidence based on age? Needs refinement.
            confidence = min(0.5 + (age_days / 365.0) * 0.4, 0.9) # Max 0.9 confidence

            yield Suggestion(
                id=self._generate_suggestion_id(SUGGESTION_OLD_FILE, details),
                suggestion_type=SUGGESTION_OLD_FILE,
                description=desc,
                details=details,
                estimated_size_bytes=item.size_bytes,
                confidence=confidence,
                rationale=rationale,
                data=item
            )

    def _generate_large_file_suggestions(self, large_files: List[ScannedItem]) -> Iterator[Suggestion]:
        """Generates suggestions for large files."""
        # Avoid suggesting deletion for files also marked as 'old' to prevent duplicate suggestions?
        # Or let the UI handle grouping? Let's generate both for now.
        for item in large_files:
            size_hr = human_readable_size(item.size_bytes)
            details = str(item.path)
            desc = f"Review large file ({size_hr})"
            rationale = f"File size ({size_hr}) exceeds threshold. Last accessed: {time.ctime(item.last_accessed)}."
            # Confidence is lower for large files as size alone isn't a great indicator for deletion
            confidence = 0.3

            yield Suggestion(
                id=self._generate_suggestion_id(SUGGESTION_LARGE_FILE, details),
                suggestion_type=SUGGESTION_LARGE_FILE,
                description=desc,
                details=details,
                estimated_size_bytes=item.size_bytes,
                confidence=confidence,
                rationale=rationale,
                data=item
            )

    def _generate_orphan_package_suggestions(self, orphans: List[PackageInfo]) -> Iterator[Suggestion]:
        """Generates suggestions for removing orphan packages."""
        total_size = sum(pkg.size_bytes for pkg in orphans)
        if not orphans:
            return

        # Suggest removing all orphans together
        details = ", ".join(sorted([pkg.name for pkg in orphans])) # Sort for deterministic ID
        size_hr = human_readable_size(total_size)
        desc = f"Remove {len(orphans)} orphan packages ({size_hr})"
        rationale = "These packages were installed as dependencies but are no longer required by any installed package."
        confidence = 0.8 # Generally safe to remove orphans

        yield Suggestion(
            id=self._generate_suggestion_id(SUGGESTION_ORPHAN_PACKAGE, "all_orphans"),
            suggestion_type=SUGGESTION_ORPHAN_PACKAGE,
            description=desc,
            details=details, # List all packages in details
            estimated_size_bytes=total_size,
            confidence=confidence,
            rationale=rationale,
            data=orphans # Pass the list of PackageInfo objects
        )

    def _generate_duplicate_set_suggestions(self, duplicate_sets: List[DuplicateSet]) -> Iterator[Suggestion]:
        """Generates suggestions for duplicate file sets."""
        for dup_set in duplicate_sets:
            num_files = len(dup_set.paths)
            # Potential saving is (num_files - 1) * size_bytes
            potential_saving = (num_files - 1) * dup_set.size_bytes
            if potential_saving <= 0: continue # Skip if only one file somehow ended up here

            size_hr = human_readable_size(dup_set.size_bytes)
            total_size_hr = human_readable_size(potential_saving)
            # Show first few paths in details for context
            paths_preview = ", ".join([p.name for p in dup_set.paths[:3]])
            if len(dup_set.paths) > 3: paths_preview += ", ..."
            details = f"{num_files} files ({size_hr} each). Hash: {dup_set.file_hash[:8]}... Locations: {paths_preview}"
            desc = f"Remove {num_files - 1} duplicate files (Save {total_size_hr})"
            rationale = f"Found {num_files} identical files based on content hash. Keeping one copy is usually sufficient."
            confidence = 0.7 # High confidence, but user should verify which one to keep if needed

            yield Suggestion(
                id=self._generate_suggestion_id(SUGGESTION_DUPLICATE_SET, dup_set.file_hash),
                suggestion_type=SUGGESTION_DUPLICATE_SET,
                description=desc,
                details=details,
                estimated_size_bytes=potential_saving,
                confidence=confidence,
                rationale=rationale,
                data=dup_set # Pass the DuplicateSet object
            )

    def _parse_pkg_filename(self, filename: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Parses name, version, arch from pacman cache filename."""
        # Example: package-name-1.2.3-4-x86_64.pkg.tar.zst
        match = re.match(r'^(.*?)-(\d.*?)-(\d+)-(.*?)\.pkg\.tar\.(?:zst|xz|gz|bz2)$', filename)
        if match:
            name = match.group(1)
            version = f"{match.group(2)}-{match.group(3)}" # Combine pkgver and pkgrel
            arch = match.group(4)
            return name, version, arch
            return None, None, None

    def _parse_pkg_filename(self, filename: str) -> Tuple[Optional[str], Optional[str], Optional[str]]: # Added commas
        """Parses name, version, arch from pacman cache filename."""
        # Example: package-name-1.2.3-4-x86_64.pkg.tar.zst
        match = re.match(r'^(.*?)-(\d.*?)-(\d+)-(.*?)\.pkg\.tar\.(?:zst|xz|gz|bz2)$', filename)
        if match:
            name = match.group(1)
            version = f"{match.group(2)}-{match.group(3)}" # Combine pkgver and pkgrel
            arch = match.group(4)
            return name, version, arch
        return None, None, None

    def _generate_pacman_cache_suggestions(self, cache_files: List[ScannedItem]) -> Iterator[Suggestion]:
        """Generates suggestions for cleaning the pacman cache."""
        if not cache_files:
            return

        packages: Dict[str, List[Tuple[str, Path, int]]] = {} # {pkg_name: [(version, path, size)]}
        total_size = 0

        for item in cache_files:
            total_size += item.size_bytes
            name, version, arch = self._parse_pkg_filename(item.path.name)
            if name and version:
                if name not in packages:
                    packages[name] = []
                packages[name].append((version, item.path, item.size_bytes))

        # Sort versions within each package (using pacman's version comparison if possible, simple string sort for now)
        # A proper version sort requires a library or complex logic. `vercmp` utility or python library needed.
        # Simple sort as fallback:
        for name in packages:
            packages[name].sort(key=lambda x: x[0], reverse=True) # Sort descending by version string

        files_to_remove: List[Tuple[Path, int]] = []
        for name, versions in packages.items():
            # Keep the latest 'pacman_cache_keep' versions
            keep_count = max(1, self.pacman_cache_keep) # Always keep at least 1? Or 0? Configurable? Let's keep >=1.
            if len(versions) > keep_count:
                files_to_remove.extend([(path, size) for version, path, size in versions[keep_count:]])

        if not files_to_remove:
            logger.info("No pacman cache files found to remove based on 'keep' setting.")
            return

        removable_size = sum(size for path, size in files_to_remove)
        size_hr = human_readable_size(removable_size)
        num_files = len(files_to_remove)

        details = f"{num_files} older package versions. Total cache size: {human_readable_size(total_size)}"
        desc = f"Clean pacman cache: Remove {num_files} older versions (Save {size_hr})"
        rationale = f"Keep the latest {self.pacman_cache_keep} version(s) of each package and remove older ones."
        confidence = 0.9 # Generally very safe

        yield Suggestion(
            id=self._generate_suggestion_id(SUGGESTION_PACMAN_CACHE, "older_versions"),
            suggestion_type=SUGGESTION_PACMAN_CACHE,
            description=desc,
            details=details,
            estimated_size_bytes=removable_size,
            confidence=confidence,
            rationale=rationale,
            data=[item[0] for item in files_to_remove] # Pass list of Paths to remove
        )

        # TODO: Add suggestion for removing uninstalled package cache files (`paccache -ruk0`) if configured


    def _generate_journal_log_suggestions(self, journal_items: List[ScannedItem]) -> Iterator[Suggestion]:
        """Generates suggestions for vacuuming journal logs."""
        if not journal_items:
            return

        # Assume only one journal item representing the total size/main dir
        journal_item = journal_items[0]
        current_size = journal_item.size_bytes
        size_hr = human_readable_size(current_size)

        vacuum_needed = False
        rationale = ""
        estimated_saving = 0
        target_size_hr = ""

        # Check size threshold first
        if self.journal_max_bytes is not None and current_size > self.journal_max_bytes:
            vacuum_needed = True
            target_size_hr = human_readable_size(self.journal_max_bytes)
            rationale = f"Journal size ({size_hr}) exceeds configured limit ({target_size_hr})."
            estimated_saving = current_size - self.journal_max_bytes
        # Check age threshold if size limit not exceeded or not set
        elif self.journal_max_seconds is not None:
            # Need a way to check age of oldest log entry - requires journalctl command, not just dir stat
            # For now, we can only suggest vacuuming based on total size.
            # TODO: Enhance collection/analysis to get oldest entry timestamp if possible.
            pass

        if vacuum_needed:
            details = f"Current size: {size_hr}. Target size: {target_size_hr}"
            desc = f"Vacuum journal logs to target size ({target_size_hr})"
            confidence = 0.8 # Generally safe, but vacuuming removes history

            yield Suggestion(
                id=self._generate_suggestion_id(SUGGESTION_JOURNAL_LOG, "vacuum_size"),
                suggestion_type=SUGGESTION_JOURNAL_LOG,
                description=desc,
                details=details,
                estimated_size_bytes=max(0, estimated_saving), # Ensure non-negative
                confidence=confidence,
                rationale=rationale,
                data={'target_size': self.journal_max_bytes, 'target_age': None} # Pass vacuum parameters
            )
        else:
             logger.info(f"Journal size ({size_hr}) is within configured limits.")


# Example Usage (requires config and db setup from DataCollector/Analysis examples)
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Use artifacts from DataCollector example
    temp_dir = Path("./temp_collector_test")
    config_file = temp_dir / "config.toml"
    db_file = temp_dir / "test_collector.db"

    if not config_file.exists() or not db_file.exists():
        print("Please run the DataCollector/Analysis examples first to create test data.")
    else:
        try:
            cfg_manager = ConfigManager(config_file)
            # Ensure analysis thresholds are set for testing
            cfg_manager.config['thresholds']['old_file'] = '1s'
            cfg_manager.config['thresholds']['large_file'] = '50'
            cfg_manager.old_file_seconds = 1
            cfg_manager.large_file_bytes = 50
            # Add dummy orphan/cache/journal data to DB for testing recommendations
            db_manager = DatabaseManager(db_file)
            scan_id = db_manager.start_scan() # Get a valid scan_id
            if scan_id:
                 # Add dummy orphan
                 db_manager.add_packages_batch([PackageInfo(name='dummy-orphan', version='1.0', size_bytes=12345, is_orphan=True)], scan_id)
                 # Add dummy cache files (simulate multiple versions)
                 cache_items = [
                     ScannedItem(path=Path('/var/cache/pacman/pkg/test-pkg-1.0-1-any.pkg.tar.zst'), size_bytes=1000, item_type=ITEM_TYPE_PACMAN_CACHE, last_accessed=time.time(), last_modified=time.time()-86400*2),
                     ScannedItem(path=Path('/var/cache/pacman/pkg/test-pkg-1.1-1-any.pkg.tar.zst'), size_bytes=1100, item_type=ITEM_TYPE_PACMAN_CACHE, last_accessed=time.time(), last_modified=time.time()-86400*1),
                     ScannedItem(path=Path('/var/cache/pacman/pkg/another-pkg-2.0-1-x86_64.pkg.tar.xz'), size_bytes=2000, item_type=ITEM_TYPE_PACMAN_CACHE, last_accessed=time.time(), last_modified=time.time()-86400*5),
                 ]
                 db_manager.add_scanned_items_batch(cache_items, scan_id)
                 # Add dummy journal entry (exceeding a hypothetical limit)
                 cfg_manager.config['arch']['journal_max_disk_size'] = '1k' # Set low limit for testing
                 cfg_manager.journal_max_bytes = 1024
                 journal_item = ScannedItem(path=Path('/var/log/journal/some-id'), size_bytes=5000, item_type=ITEM_TYPE_JOURNAL_LOG, last_accessed=time.time(), last_modified=time.time())
                 db_manager.add_scanned_items_batch([journal_item], scan_id)
                 db_manager.end_scan(scan_id, 5) # Mark scan as ended

            analyzer = AnalysisEngine(cfg_manager, db_manager)
            analysis_results = analyzer.analyze_all()

            recommender = RecommendationEngine(cfg_manager)

            print("\n--- Generating Recommendations ---")
            suggestions = recommender.generate_suggestions(analysis_results)
            print("--- Recommendations Generated ---")

            print("\n--- Generated Suggestions ---")
            if not suggestions:
                print("No suggestions generated.")
            for sugg in suggestions:
                print(f"\nID: {sugg.id}")
                print(f"  Type: {sugg.suggestion_type}")
                print(f"  Desc: {sugg.description}")
                print(f"  Details: {sugg.details}")
                print(f"  Size: {human_readable_size(sugg.estimated_size_bytes)}")
                print(f"  Confidence: {sugg.confidence:.2f}")
                print(f"  Rationale: {sugg.rationale}")
                # print(f"  Data: {sugg.data}") # Can be verbose

        except Exception as e:
            logger.exception("Error during RecommendationEngine example")
        finally:
             if 'db_manager' in locals():
                 db_manager.close()

import time
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set, Any

from ..core.models import ScannedItem, PackageInfo, DuplicateSet
from ..db.database import DatabaseManager
from ..modules.config_manager import ConfigManager
from ..utils.helpers import parse_duration, parse_size
from .collection import ITEM_TYPE_FILE, ITEM_TYPE_PACMAN_CACHE, ITEM_TYPE_JOURNAL_LOG

logger = logging.getLogger(__name__)


class AnalysisEngine:
    """Analyzes collected data to identify cleanup opportunities."""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        """
        Initializes the AnalysisEngine with configuration and database access.
        Args:
            config_manager: Manages configuration settings.
            db_manager: Manages database interactions.
        """
        self.config = config_manager
        self.db = db_manager
        self._load_thresholds()

    def _load_thresholds(self):
        """Loads and parses analysis thresholds from the configuration."""
        self.old_file_threshold_str = self.config.get('thresholds.old_file', '3m')
        self.large_file_threshold_str = self.config.get('thresholds.large_file', '500M')
        self.duplicate_min_size_str = self.config.get('duplicates.min_size', '1M')

        self.old_file_seconds = parse_duration(self.old_file_threshold_str)
        self.large_file_bytes = parse_size(self.large_file_threshold_str)
        self.duplicate_min_bytes = parse_size(self.duplicate_min_size_str)

        if self.old_file_seconds is None:
            logger.warning(f"Invalid old_file threshold '{self.old_file_threshold_str}', defaulting to 90 days.")
            self.old_file_seconds = 90 * 86400
        if self.large_file_bytes is None:
            logger.warning(f"Invalid large_file threshold '{self.large_file_threshold_str}', defaulting to 500MB.")
            self.large_file_bytes = 500 * 1024 * 1024
        if self.duplicate_min_bytes is None:
            logger.warning(f"Invalid duplicate min_size '{self.duplicate_min_size_str}', defaulting to 1MB.")
            self.duplicate_min_bytes = 1 * 1024 * 1024

        logger.info(f"Analysis thresholds: Old files > {self.old_file_seconds}s, Large files > {self.large_file_bytes}b, Duplicates > {self.duplicate_min_bytes}b")

    def analyze_all(self) -> Dict[str, List[Any]]:
        """
        Performs all configured analyses on the data in the database.
        Returns:
            A dictionary with analysis types as keys and lists of items as values.
        """
        logger.info("Starting analysis of collected data...")
        results = {}

        all_files = self.db.get_scanned_items(item_type=ITEM_TYPE_FILE)
        results['old_files'] = self._find_old_files(all_files)
        results['large_files'] = self._find_large_files(all_files)
        logger.info(f"Found {len(results['old_files'])} old files, {len(results['large_files'])} large files.")

        if self.config.get('arch.remove_orphans', False):
            results['orphan_packages'] = self.db.get_packages(orphans_only=True)
            logger.info(f"Found {len(results['orphan_packages'])} orphan packages.")
        else:
            results['orphan_packages'] = []

        if self.config.get('duplicates.enabled', False):
            results['duplicate_sets'] = self._find_duplicate_sets()
            logger.info(f"Found {len(results['duplicate_sets'])} duplicate sets.")
        else:
            results['duplicate_sets'] = []

        if self.config.get('arch.clean_pacman_cache', False):
            results['pacman_cache_files'] = self.db.get_scanned_items(item_type=ITEM_TYPE_PACMAN_CACHE)
            logger.info(f"Found {len(results['pacman_cache_files'])} pacman cache files.")
        else:
            results['pacman_cache_files'] = []

        if self.config.get('arch.clean_journal', False):
            results['journal_logs'] = self.db.get_scanned_items(item_type=ITEM_TYPE_JOURNAL_LOG)
            logger.info(f"Found {len(results['journal_logs'])} journal log entries.")
        else:
            results['journal_logs'] = []

        logger.info("Analysis complete.")
        return results

    def _find_old_files(self, files: List[ScannedItem]) -> List[ScannedItem]:
        """Filters files to find those older than the configured threshold."""
        if self.old_file_seconds is None:
            return []
        cutoff_time = time.time() - self.old_file_seconds
        return [f for f in files if f.last_accessed < cutoff_time]

    def _find_large_files(self, files: List[ScannedItem]) -> List[ScannedItem]:
        """Filters files to find those larger than the configured threshold."""
        if self.large_file_bytes is None:
            return []
        large_files = [f for f in files if f.size_bytes >= self.large_file_bytes]
        large_files.sort(key=lambda x: x.size_bytes, reverse=True)
        return large_files

    def _find_duplicate_sets(self) -> List[DuplicateSet]:
        """Finds and validates sets of duplicate files from the database."""
        if self.duplicate_min_bytes is None:
            return []

        potential_hashes = self.db.find_potential_duplicates(self.duplicate_min_bytes)
        duplicate_sets = []

        for file_hash, size_bytes, _ in potential_hashes:
            if not file_hash:
                continue

            paths = self.db.get_files_by_hash(file_hash)
            valid_paths = [p for p in paths if p.exists()]

            if len(valid_paths) > 1:
                duplicate_sets.append(DuplicateSet(
                    file_hash=file_hash,
                    paths=valid_paths,
                    size_bytes=size_bytes,
                    total_size_bytes=size_bytes * len(valid_paths)
                ))
            else:
                logger.debug(f"Duplicate set for hash {file_hash[:8]} has <= 1 valid file, skipping.")

        duplicate_sets.sort(key=lambda x: x.total_size_bytes, reverse=True)
        return duplicate_sets


# Example Usage (requires config and db setup from DataCollector example)
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Use artifacts from DataCollector example
    temp_dir = Path("./temp_collector_test")
    config_file = temp_dir / "config.toml"
    db_file = temp_dir / "test_collector.db"

    if not config_file.exists() or not db_file.exists():
        print("Please run the DataCollector example (collection.py) first to create test data.")
    else:
        try:
            cfg_manager = ConfigManager(config_file)
            # Modify config for testing analysis thresholds if needed
            cfg_manager.config['thresholds']['old_file'] = '1s' # Make files old quickly
            cfg_manager.config['thresholds']['large_file'] = '50' # Bytes
            cfg_manager.old_file_seconds = 1
            cfg_manager.large_file_bytes = 50

            db_manager = DatabaseManager(db_file)
            analyzer = AnalysisEngine(cfg_manager, db_manager)

            print("\n--- Running Analysis ---")
            analysis_results = analyzer.analyze_all()
            print("--- Analysis Finished ---")

            print("\n--- Analysis Results ---")
            for category, items in analysis_results.items():
                print(f"\nCategory: {category} ({len(items)} items)")
                if items:
                    # Print details for a few items per category
                    for item in items[:5]:
                        if isinstance(item, ScannedItem):
                            print(f"  - {item.path.name} (Size: {item.size_bytes}b, Accessed: {time.ctime(item.last_accessed)})")
                        elif isinstance(item, PackageInfo):
                            print(f"  - {item.name} (Version: {item.version}, Size: {item.size_bytes}b)")
                        elif isinstance(item, DuplicateSet):
                            print(f"  - Hash: {item.file_hash[:8]}... (Size: {item.size_bytes}b x {len(item.paths)} files = {item.total_size_bytes}b)")
                            for p in item.paths[:2]: print(f"    - {p.name}")
                            if len(item.paths) > 2: print("    - ...")
                        else:
                            print(f"  - {item}") # Fallback for other types

        except Exception as e:
            logger.exception("Error during AnalysisEngine example")
        finally:
             if 'db_manager' in locals():
                 db_manager.close()

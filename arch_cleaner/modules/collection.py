import os
import stat
import time
import logging
import hashlib
import re
from pathlib import Path
from typing import List, Optional, Iterator, Dict, Tuple

from ..core.models import ScannedItem, PackageInfo
from ..db.database import DatabaseManager
from ..modules.config_manager import ConfigManager
from ..utils.helpers import run_command, is_path_excluded, calculate_hash, parse_size

logger = logging.getLogger(__name__)

ITEM_TYPE_FILE = "file"
ITEM_TYPE_DIR = "directory"
ITEM_TYPE_CACHE = "cache"
ITEM_TYPE_LOG = "log"
ITEM_TYPE_PACMAN_CACHE = "pacman_cache"
ITEM_TYPE_JOURNAL_LOG = "journal_log"
ITEM_TYPE_PACKAGE = "package"


class DataCollector:
    """Collects data about system storage usage."""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        """
        Initializes the DataCollector.
        Args:
            config_manager: Manages configuration settings.
            db_manager: Manages database interactions.
        """
        self.config = config_manager
        self.db = db_manager
        self.current_scan_id: Optional[int] = None

    def start_collection(self) -> bool:
        """Initiates the data collection process by creating a new scan record."""
        self.current_scan_id = self.db.start_scan()
        if not self.current_scan_id:
            logger.error("Failed to start a new scan record in the database.")
            return False
        logger.info(f"Starting data collection for scan ID: {self.current_scan_id}")
        self.db.clear_scan_data(self.current_scan_id)
        return True

    def finish_collection(self, items_found: int, errors: Optional[str] = None):
        """Finalizes the data collection by updating the scan record."""
        if self.current_scan_id:
            self.db.end_scan(self.current_scan_id, items_found, errors)
            logger.info(f"Finished data collection for scan ID: {self.current_scan_id}. Items found: {items_found}")
        else:
            logger.warning("finish_collection called without a valid scan ID.")

    def collect_all(self, force_rescan: bool = False, target_directory: Optional[str] = None):
        """
        Orchestrates the collection of all data types based on configuration.

        Args:
            force_rescan: If True, forces recalculation/recollection where applicable.
            target_directory: If provided, scan only this directory instead of config paths,
                              and skip system-wide collections (packages, cache, journal).
        """
        if not self.start_collection():
            return

        total_items_found = 0
        errors = []
        is_targeted_scan = bool(target_directory)

        scan_paths = self._get_scan_paths(target_directory)
        if not scan_paths and is_targeted_scan:
            logger.error(f"Target directory not valid: {target_directory}")
            self.finish_collection(0, errors=f"Target directory not valid: {target_directory}")
            return

        exclude_patterns = self.config.get_exclude_patterns()
        should_hash = self.config.get('duplicates.enabled', False)
        min_hash_size = parse_size(self.config.get('duplicates.min_size', '1M')) or 1024 * 1024

        total_items_found += self._scan_and_collect_filesystem(scan_paths, exclude_patterns, should_hash, min_hash_size, errors)

        if not is_targeted_scan:
            self._collect_system_wide_data(errors)

        if should_hash:
            self._mark_duplicates_in_db(min_hash_size, errors)

        self.finish_collection(total_items_found, errors="\n".join(errors) if errors else None)

    def _get_scan_paths(self, target_directory: Optional[str]) -> List[Path]:
        """Determines the paths to scan based on input."""
        if target_directory:
            target_path = Path(target_directory).expanduser().resolve()
            if target_path.is_dir():
                logger.info(f"Performing targeted scan on directory: {target_path}")
                return [target_path]
            return []
        logger.info("Performing general scan based on config paths.")
        return self.config.get_scan_paths()

    def _scan_and_collect_filesystem(self, scan_paths: List[Path], exclude_patterns: List[str], should_hash: bool, min_hash_size: int, errors: List[str]) -> int:
        """Scans the filesystem and adds items to the database."""
        if not scan_paths:
            logger.warning("No valid paths to scan.")
            return 0

        try:
            items = list(self._scan_filesystem(scan_paths, exclude_patterns, should_hash, min_hash_size))
            if items:
                self.db.add_scanned_items_batch(items, self.current_scan_id)
                logger.info(f"Collected {len(items)} filesystem items.")
                return len(items)
        except Exception as e:
            logger.error(f"Error during filesystem scan: {e}", exc_info=True)
            errors.append(f"Filesystem Scan Error: {e}")
        return 0

    def _collect_system_wide_data(self, errors: List[str]):
        """Collects system-wide data like packages, caches, and logs."""
        logger.info("Running system-wide collections...")
        collection_tasks = {
            "package_info": (lambda: list(self._collect_package_info()), self.db.add_packages_batch),
            "pacman_cache": (lambda: list(self._collect_pacman_cache_info()), self.db.add_scanned_items_batch),
            "journal_info": (lambda: list(self._collect_journal_info()), self.db.add_scanned_items_batch),
        }

        for name, (collect_func, add_func) in collection_tasks.items():
            try:
                items = collect_func()
                if items:
                    add_func(items, self.current_scan_id)
                    logger.info(f"Collected {len(items)} {name.replace('_', ' ')} items.")
            except Exception as e:
                logger.error(f"Error collecting {name}: {e}", exc_info=True)
                errors.append(f"{name.capitalize()} Error: {e}")

    def _mark_duplicates_in_db(self, min_hash_size: int, errors: List[str]):
        """Marks duplicate files in the database."""
        try:
            super()._mark_duplicates_in_db(min_hash_size)
        except Exception as e:
            logger.error(f"Error marking duplicates: {e}", exc_info=True)
            errors.append(f"Duplicate Marking Error: {e}")

    def _scan_filesystem(self, paths_to_scan: List[Path], exclude_patterns: List[str], calculate_hashes: bool, min_hash_size: int) -> Iterator[ScannedItem]:
        """
        Walks through specified paths, collects file/dir info, and yields ScannedItem objects.
        """
        processed_paths = set()
        for start_path in paths_to_scan:
            if not start_path.exists():
                logger.warning(f"Scan path does not exist: {start_path}")
                continue

            if start_path.is_file():
                if start_path not in processed_paths and not is_path_excluded(start_path, exclude_patterns):
                    yield self._process_path(start_path, calculate_hashes, min_hash_size)
                    processed_paths.add(start_path)
                continue

            for root, dirs, files in os.walk(start_path, topdown=True, onerror=lambda e: logger.warning(f"Error accessing {e.filename}: {e.strerror}")):
                current_dir_path = Path(root)
                dirs[:] = [d for d in dirs if not is_path_excluded(current_dir_path / d, exclude_patterns)]

                for filename in files:
                    file_path = current_dir_path / filename
                    if file_path not in processed_paths and not is_path_excluded(file_path, exclude_patterns):
                        yield self._process_path(file_path, calculate_hashes, min_hash_size)
                        processed_paths.add(file_path)

    def _process_path(self, path: Path, calculate_hashes: bool, min_hash_size: int) -> Optional[ScannedItem]:
        """Gets metadata for a path and returns a ScannedItem."""
        try:
            stat_result = path.stat()
        except (FileNotFoundError, OSError) as e:
            logger.warning(f"Could not process path {path}: {e}")
            return None

        is_dir = stat.S_ISDIR(stat_result.st_mode)
        item_type = ITEM_TYPE_DIR if is_dir else ITEM_TYPE_FILE
        size = stat_result.st_size

        if ".log" in path.name.lower():
            item_type = ITEM_TYPE_LOG
        elif ".cache" in path.parts or "cache" in path.name.lower():
            item_type = ITEM_TYPE_CACHE

        extra_info = {}
        if not is_dir and calculate_hashes and size >= min_hash_size:
            extra_info['hash'] = calculate_hash(path)

        return ScannedItem(
            path=path.resolve(),
            size_bytes=size,
            last_accessed=stat_result.st_atime,
            last_modified=stat_result.st_mtime,
            item_type=item_type,
            extra_info=extra_info
        )

    def _collect_package_info(self) -> Iterator[PackageInfo]:
        """Collects information about installed pacman packages."""
        logger.info("Collecting package information...")
        orphan_cmd = ['pacman', '-Qtdq']
        result_orphans = run_command(orphan_cmd, capture_output=True, check=False)
        orphans = set(result_orphans.stdout.strip().split('\n')) if result_orphans.returncode == 0 and result_orphans.stdout else set()

        list_cmd = ['pacman', '-Q']
        result_list = run_command(list_cmd, capture_output=True, check=False)
        if result_list.returncode != 0:
            logger.error("Failed to list installed packages using pacman -Q")
            return

        for line in result_list.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                name, version = line.split(' ', 1)
                info_cmd = ['pacman', '-Qi', name]
                result_info = run_command(info_cmd, capture_output=True, check=False)
                if result_info.returncode != 0:
                    logger.warning(f"Failed to get info for package: {name}")
                    continue

                pkg_data = self._parse_pacman_qi(result_info.stdout)
                if not pkg_data:
                    logger.warning(f"Could not parse pacman -Qi output for {name}")
                    continue

                yield PackageInfo(
                    name=name,
                    version=version,
                    size_bytes=pkg_data.get('size', 0),
                    description=pkg_data.get('description'),
                    install_date=pkg_data.get('install_date'),
                    is_orphan=name in orphans,
                    is_dependency=pkg_data.get('is_dependency', False),
                    required_by=pkg_data.get('required_by', []),
                    optional_for=pkg_data.get('optional_for', [])
                )
            except Exception as e:
                logger.error(f"Error processing package line '{line}': {e}", exc_info=True)

    def _parse_pacman_qi(self, output: str) -> dict:
        """Parses the output of 'pacman -Qi <package>'."""
        data = {}
        key_map = {
            "Description": "description", "Installed Size": "size", "Install Date": "install_date",
            "Required By": "required_by", "Optional For": "optional_for", "Install Reason": "install_reason"
        }
        list_keys = {"required_by", "optional_for"}

        for line in output.strip().split('\n'):
            if ':' in line:
                key_str, value = line.split(':', 1)
                key = key_map.get(key_str.strip())
                if key:
                    value = value.strip()
                    if key in list_keys:
                        data[key] = [v.strip() for v in value.split()] if value != "None" else []
                    elif key == 'size':
                        data[key] = parse_size(value) or 0
                    elif key == 'install_date':
                        try:
                            data[key] = time.mktime(time.strptime(value.replace(" UTC", ""), "%a %d %b %Y %I:%M:%S %p"))
                        except ValueError:
                            data[key] = None
                    else:
                        data[key] = value if value != "None" else None

        data['is_dependency'] = bool(data.get('required_by')) or data.get('install_reason') == 'Installed as a dependency for another package'
        return data

    def _collect_pacman_cache_info(self) -> Iterator[ScannedItem]:
        """Collects info about files in the pacman cache directory."""
        cache_dir = Path('/var/cache/pacman/pkg/')
        logger.info(f"Scanning pacman cache directory: {cache_dir}")
        if not cache_dir.is_dir():
            logger.warning(f"Pacman cache directory not found: {cache_dir}")
            return

        exclude_patterns = self.config.get_exclude_patterns()
        for item_path in cache_dir.glob('*.pkg.tar.*'):
            if item_path.is_file() and not is_path_excluded(item_path, exclude_patterns):
                try:
                    stat_result = item_path.stat()
                    yield ScannedItem(
                        path=item_path.resolve(),
                        size_bytes=stat_result.st_size,
                        last_accessed=stat_result.st_atime,
                        last_modified=stat_result.st_mtime,
                        item_type=ITEM_TYPE_PACMAN_CACHE,
                        extra_info={}
                    )
                except OSError as e:
                    logger.warning(f"Could not process pacman cache file {item_path}: {e}")

    def _collect_journal_info(self) -> Iterator[ScannedItem]:
        """Collects info about systemd journal files."""
        logger.info("Collecting journal log information...")
        journal_path = Path('/var/log/journal')
        if not journal_path.is_dir():
            journal_path = Path('/run/log/journal')
        if not journal_path.is_dir():
            logger.info("No systemd journal directory found.")
            return

        cmd = ['journalctl', '--disk-usage']
        result = run_command(cmd, capture_output=True, check=False)
        total_size = 0
        source = 'scan'

        if result.returncode == 0 and result.stdout:
            match = re.search(r'take up\s+([\d.]+[BKMGT])', result.stdout)
            if match:
                total_size = parse_size(match.group(1)) or 0
                source = 'journalctl'
        else:
            logger.warning(f"journalctl --disk-usage failed. Falling back to directory scan.")
            try:
                total_size = sum(f.stat().st_size for f in journal_path.glob('**/*') if f.is_file())
            except OSError as e:
                logger.warning(f"Could not scan journal directory {journal_path} for size: {e}")
                return

        try:
            stat_result = journal_path.stat()
            yield ScannedItem(
                path=journal_path.resolve(),
                size_bytes=total_size,
                last_accessed=stat_result.st_atime,
                last_modified=stat_result.st_mtime,
                item_type=ITEM_TYPE_JOURNAL_LOG,
                extra_info={'source': source}
            )
        except OSError as e:
            logger.warning(f"Could not stat journal directory {journal_path}: {e}")

    def _mark_duplicates_in_db(self, min_size: int):
        """Identifies and marks duplicate files in the database."""
        logger.info(f"Identifying and marking duplicates (min size: {min_size} bytes)...")
        potential_dups = self.db.find_potential_duplicates(min_size)
        if not potential_dups:
            logger.info("No potential duplicate hashes found.")
            return

        logger.info(f"Found {len(potential_dups)} potential duplicate hashes.")
        if not self.db.conn:
            return

        try:
            with self.db.conn:
                for file_hash, _, num_files in potential_dups:
                    if file_hash:
                        self.db.mark_duplicates(file_hash)
            logger.info(f"Marked files for {len(potential_dups)} duplicate sets.")
        except sqlite3.Error as e:
            logger.error(f"Database error while marking duplicates: {e}", exc_info=True)
            raise


# Example Usage (requires config and db setup)
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Dummy Config/DB for testing
    temp_dir = Path("./temp_collector_test")
    temp_dir.mkdir(exist_ok=True)
    config_file = temp_dir / "config.toml"
    db_file = temp_dir / "test_collector.db"

    # Create dummy config
    config_file.write_text(f"""
[paths]
scan = ["{str(temp_dir)}"] # Scan the temp dir itself
exclude = ["*.log", "*/ignore/*"]

[duplicates]
enabled = true
min_size = "10" # Bytes

[arch]
clean_pacman_cache = false # Assume not running as root
remove_orphans = false
clean_journal = false
    """)

    # Create dummy files
    (temp_dir / "file1.txt").write_text("Hello")
    (temp_dir / "file2.txt").write_text("World")
    (temp_dir / "file3_dup.txt").write_text("Hello") # Duplicate content
    (temp_dir / "large_file.bin").write_text("A"*100)
    (temp_dir / "ignored.log").write_text("Log data")
    (temp_dir / "ignore").mkdir(exist_ok=True)
    (temp_dir / "ignore" / "ignored_file.txt").write_text("Secret")


    if db_file.exists():
        db_file.unlink()

    try:
        cfg_manager = ConfigManager(config_file)
        db_manager = DatabaseManager(db_file)
        collector = DataCollector(cfg_manager, db_manager)

        print("--- Starting Collection ---")
        collector.collect_all()
        print("--- Collection Finished ---")

        print("\n--- Retrieving Data ---")
        all_items = db_manager.get_scanned_items()
        print(f"Total items in DB: {len(all_items)}")
        for item in all_items:
            print(f"- {item.path.name} ({item.item_type}, {item.size_bytes}b, hash: {item.extra_info.get('hash', 'N/A')[:8]}..., dup: {item.extra_info.get('is_duplicate', False)})")

        # Verify duplicates were marked
        dup_items = [i for i in all_items if i.extra_info.get('is_duplicate')]
        print(f"\nDuplicate items marked: {len(dup_items)}")


    except Exception as e:
        logger.exception("Error during DataCollector example")
    finally:
        # Clean up
        if db_file.exists():
            db_manager.close() # Ensure connection is closed before unlinking
            # db_file.unlink() # Sometimes fails immediately after close on some systems
            pass
        # shutil.rmtree(temp_dir) # Clean up temp dir
        print(f"\nTest artifacts are in {temp_dir}")
        print(f"Database is at {db_file}")

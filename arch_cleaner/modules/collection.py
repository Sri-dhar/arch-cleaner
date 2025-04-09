import os
import stat
import time
import logging
import hashlib
import re # Added for parsing pacman output
from pathlib import Path
from typing import List, Optional, Iterator, Dict, Tuple # Added Dict, Tuple

# Project imports
from ..core.models import ScannedItem, PackageInfo
from ..db.database import DatabaseManager
from ..modules.config_manager import ConfigManager
from ..utils.helpers import run_command, is_path_excluded, calculate_hash, parse_size, human_readable_size # Added human_readable_size

logger = logging.getLogger(__name__)

# Define constants for item types used in the database
ITEM_TYPE_FILE = "file"
ITEM_TYPE_DIR = "directory"
ITEM_TYPE_CACHE = "cache" # Generic cache file/dir
ITEM_TYPE_LOG = "log"
ITEM_TYPE_PACMAN_CACHE = "pacman_cache"
ITEM_TYPE_JOURNAL_LOG = "journal_log"
ITEM_TYPE_PACKAGE = "package" # Represents the package itself, stored separately

class DataCollector:
    """Collects data about system storage usage."""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.current_scan_id: Optional[int] = None

    def start_collection(self) -> bool:
        """Initiates the data collection process."""
        self.current_scan_id = self.db.start_scan()
        if not self.current_scan_id:
            logger.error("Failed to start a new scan record in the database.")
            return False
        logger.info(f"Starting data collection for scan ID: {self.current_scan_id}")
        # Clear previous data for this scan ID if it somehow exists (shouldn't happen)
        # Or maybe clear *all* old data before starting? Configurable?
        # For now, assume we add/replace based on primary keys.
        # Let's clear data associated with *this* scan ID before adding new stuff, just in case.
        self.db.clear_scan_data(self.current_scan_id)
        return True

    def finish_collection(self, items_found: int, errors: Optional[str] = None):
        """Finalizes the data collection process."""
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
        scan_paths: List[Path] = []
        is_targeted_scan = False

        if target_directory:
            target_path = Path(target_directory).expanduser().resolve()
            if target_path.is_dir():
                scan_paths = [target_path]
                is_targeted_scan = True
                logger.info(f"Performing targeted scan on directory: {target_path}")
            else:
                logger.error(f"Target directory specified but not found or not a directory: {target_path}")
                errors.append(f"Target directory not valid: {target_path}")
                self.finish_collection(0, errors="\n".join(errors))
                return # Stop if target directory is invalid
        else:
            # Use paths from config for a general scan
            scan_paths = self.config.get_scan_paths()
            logger.info(f"Performing general scan based on config paths: {scan_paths}")


        exclude_patterns = self.config.get_exclude_patterns()
        should_hash = self.config.get('duplicates.enabled', False)
        min_hash_size_str = self.config.get('duplicates.min_size', '1M')
        min_hash_size = parse_size(min_hash_size_str) if min_hash_size_str else 1024*1024 # Default 1MB

        # Only log scan paths if they exist (could be empty if target dir was invalid but we didn't return somehow)
        if scan_paths:
            logger.info(f"Scanning paths: {[str(p) for p in scan_paths]}")
        logger.info(f"Excluding patterns: {exclude_patterns}")
        logger.info(f"Hashing enabled: {should_hash} (min size: {min_hash_size_str})")

        # 1. Scan Filesystem (Always run this part)
        try:
            if not scan_paths:
                 logger.warning("No valid paths to scan.")
            else:
                # Indent this block
                items = list(self._scan_filesystem(scan_paths, exclude_patterns, should_hash, min_hash_size or 0))
                if items:
                        self.db.add_scanned_items_batch(items, self.current_scan_id)
                        total_items_found += len(items)
                        logger.info(f"Collected {len(items)} filesystem items.")
        except Exception as e:
            logger.error(f"Error during filesystem scan: {e}", exc_info=True)
            errors.append(f"Filesystem Scan Error: {e}")

        # --- System-wide collections (Skip if target_directory is specified) ---

        if not is_targeted_scan:
            logger.info("Running system-wide collections...")

            # 2. Collect Package Info
            # Always collect package info for now, analysis can decide if it's relevant
            # if self.config.get('arch.remove_orphans', False):
            try:
                packages = list(self._collect_package_info())
                if packages:
                    self.db.add_packages_batch(packages, self.current_scan_id)
                    # Don't add packages to total_items_found, they are stored separately
                    logger.info(f"Collected info for {len(packages)} packages.")
            except Exception as e:
                logger.error(f"Error collecting package info: {e}", exc_info=True)
                errors.append(f"Package Info Error: {e}")

            # 3. Collect Pacman Cache Info (Treat as ScannedItems)
            if self.config.get('arch.clean_pacman_cache', False):
                try:
                    cache_items = list(self._collect_pacman_cache_info())
                    if cache_items:
                        self.db.add_scanned_items_batch(cache_items, self.current_scan_id)
                        total_items_found += len(cache_items)
                        logger.info(f"Collected {len(cache_items)} pacman cache items.")
                except Exception as e:
                    logger.error(f"Error collecting pacman cache info: {e}", exc_info=True)
                    errors.append(f"Pacman Cache Error: {e}")

            # 4. Collect Journal Info (Treat as ScannedItems - represent the journal dir/files)
            if self.config.get('arch.clean_journal', False):
                try:
                    journal_items = list(self._collect_journal_info())
                    if journal_items:
                        self.db.add_scanned_items_batch(journal_items, self.current_scan_id)
                        total_items_found += len(journal_items)
                        logger.info(f"Collected {len(journal_items)} journal log items.")
                except Exception as e:
                    logger.error(f"Error collecting journal info: {e}", exc_info=True)
                    errors.append(f"Journal Info Error: {e}")
        else:
             logger.info("Skipping system-wide collections due to targeted directory scan.")


        # 5. Mark Duplicates (based on hashes collected during filesystem scan - run always if enabled)
        if should_hash:
            try:
                self._mark_duplicates_in_db(min_hash_size or 0)
            except Exception as e:
                logger.error(f"Error marking duplicates: {e}", exc_info=True)
                errors.append(f"Duplicate Marking Error: {e}")


        self.finish_collection(total_items_found, errors="\n".join(errors) if errors else None)


    def _scan_filesystem(self, paths_to_scan: List[Path], exclude_patterns: List[str], calculate_hashes: bool, min_hash_size: int) -> Iterator[ScannedItem]:
        """
        Walks through specified paths, collects file/dir info, and yields ScannedItem objects.
        Optionally calculates and includes file hashes.
        """
        processed_paths = set() # Avoid processing the same path multiple times if listed or linked

        for start_path_str in paths_to_scan:
            start_path = Path(start_path_str).expanduser().resolve()
            logger.info(f"Scanning directory: {start_path}")

            if not start_path.exists():
                logger.warning(f"Scan path does not exist: {start_path}")
                continue

            # Handle case where start_path itself is a file
            if start_path.is_file():
                 if start_path in processed_paths: continue
                 if is_path_excluded(start_path, exclude_patterns): continue
                 try:
                     item = self._process_path(start_path, calculate_hashes, min_hash_size)
                     if item:
                         processed_paths.add(start_path)
                         yield item
                 except OSError as e:
                     logger.warning(f"Could not process path {start_path}: {e}")
                 continue # Move to next start_path

            # Walk the directory tree
            for root, dirs, files in os.walk(start_path, topdown=True, onerror=lambda e: logger.warning(f"Error accessing {e.filename}: {e.strerror}")):
                current_dir_path = Path(root)

                # Filter excluded directories (modifies dirs in-place for efficiency)
                dirs[:] = [d for d in dirs if not is_path_excluded(current_dir_path / d, exclude_patterns)]

                # Process files in the current directory
                for filename in files:
                    file_path = current_dir_path / filename
                    if file_path in processed_paths: continue
                    if is_path_excluded(file_path, exclude_patterns): continue

                    try:
                        item = self._process_path(file_path, calculate_hashes, min_hash_size)
                        if item:
                            processed_paths.add(file_path)
                            yield item
                    except OSError as e:
                        logger.warning(f"Could not process file {file_path}: {e}")

                # Optionally process directories themselves (after filtering)
                # Might be useful for identifying large empty dirs later?
                # For now, focus on files for cleanup suggestions.
                # if current_dir_path not in processed_paths and not is_path_excluded(current_dir_path, exclude_patterns):
                #     try:
                #         item = self._process_path(current_dir_path, False, 0) # Don't hash dirs
                #         if item:
                #             processed_paths.add(current_dir_path)
                #             yield item
                #     except OSError as e:
                #         logger.warning(f"Could not process directory {current_dir_path}: {e}")


    def _process_path(self, path: Path, calculate_hashes: bool, min_hash_size: int) -> Optional[ScannedItem]:
        """Gets metadata for a path and returns a ScannedItem."""
        try:
            # Use lstat to avoid following symlinks for basic info, but resolve for hashing/storage?
            # Let's use stat() but be aware of potential symlink loops (os.walk handles this)
            stat_result = path.stat()
        except FileNotFoundError:
            logger.debug(f"File disappeared during scan: {path}")
            return None
        except OSError as e:
            logger.warning(f"Could not stat path {path}: {e}")
            return None

        is_dir = stat.S_ISDIR(stat_result.st_mode)
        item_type = ITEM_TYPE_DIR if is_dir else ITEM_TYPE_FILE
        size = stat_result.st_size
        # Determine item type more specifically (e.g., cache, log) based on path/config?
        # This could be done here or later during analysis. Let's keep it simple for now.
        if ".log" in path.name.lower(): item_type = ITEM_TYPE_LOG
        if ".cache" in path.parts or "cache" in path.name.lower(): item_type = ITEM_TYPE_CACHE # Basic heuristic

        extra_info = {}
        if not is_dir and calculate_hashes and size >= min_hash_size:
            file_hash = calculate_hash(path)
            if file_hash:
                extra_info['hash'] = file_hash

        # Prefer access time (atime), fallback to modification time (mtime)
        access_time = stat_result.st_atime
        mod_time = stat_result.st_mtime

        return ScannedItem(
            path=path.resolve(), # Store resolved path
            size_bytes=size,
            last_accessed=access_time,
            last_modified=mod_time,
            item_type=item_type,
            extra_info=extra_info
        )

    def _collect_package_info(self) -> Iterator[PackageInfo]:
        """Collects information about installed pacman packages."""
        logger.info("Collecting package information...")
        # Get all explicitly installed packages and orphans
        # pacman -Qe: explicitly installed
        # pacman -Qt: orphans
        # pacman -Qi <pkg>: detailed info
        # pacman -Si <pkg>: repo info (install date?) - No, Qi has install date
        # pacman -Qii <pkg>: backup files, etc. (more detail)
        # pacman -Qdtq: quiet list of orphans
        # pacman -Qetq: quiet list of explicitly installed non-orphans (base install?)

        # Get orphans first
        orphan_cmd = ['pacman', '-Qtdq']
        result_orphans = run_command(orphan_cmd, capture_output=True, check=False)
        orphans = set(result_orphans.stdout.strip().split('\n')) if result_orphans.returncode == 0 and result_orphans.stdout else set()
        logger.debug(f"Found {len(orphans)} potential orphans.")

        # Get all installed packages (name and version)
        list_cmd = ['pacman', '-Q']
        result_list = run_command(list_cmd, capture_output=True, check=False)
        if result_list.returncode != 0:
            logger.error("Failed to list installed packages using pacman -Q")
            return

        packages_processed = 0
        for line in result_list.stdout.strip().split('\n'):
            if not line: continue
            try:
                name, version = line.split(' ', 1)
                info_cmd = ['pacman', '-Qi', name]
                result_info = run_command(info_cmd, capture_output=True, check=False)
                if result_info.returncode != 0:
                    logger.warning(f"Failed to get info for package: {name}")
                    continue

                # Parse pacman -Qi output (this is brittle, needs careful parsing)
                pkg_data = self._parse_pacman_qi(result_info.stdout)
                if not pkg_data:
                    logger.warning(f"Could not parse pacman -Qi output for {name}")
                    continue

                # Determine if orphan
                is_orphan = name in orphans

                # Create PackageInfo object
                yield PackageInfo(
                    name=name,
                    version=version,
                    size_bytes=pkg_data.get('size', 0),
                    description=pkg_data.get('description'),
                    install_date=pkg_data.get('install_date'),
                    is_orphan=is_orphan,
                    is_dependency=pkg_data.get('is_dependency', False), # Based on 'Required By'
                    required_by=pkg_data.get('required_by', []),
                    optional_for=pkg_data.get('optional_for', [])
                    # last_used needs heuristics later in analysis
                )
                packages_processed += 1
            except Exception as e:
                logger.error(f"Error processing package line '{line}': {e}", exc_info=True)
        logger.info(f"Processed {packages_processed} packages.")


    def _parse_pacman_qi(self, output: str) -> dict: # Changed Dict to dict
        """Parses the output of 'pacman -Qi <package>'."""
        data = {}
        current_key = None
        lines = output.strip().split('\n')

        # Regex might be more robust, but simple split for now
        key_map = {
            "Name": "name", # Already have this, but good for validation
            "Version": "version", # Already have this
            "Description": "description",
            "Architecture": "arch",
            "URL": "url",
            "Licenses": "licenses",
            "Groups": "groups",
            "Provides": "provides",
            "Depends On": "depends_on",
            "Optional Deps": "optional_deps", # Dictionary {pkg: reason}
            "Required By": "required_by",
            "Optional For": "optional_for",
            "Conflicts With": "conflicts_with",
            "Replaces": "replaces",
            "Installed Size": "size",
            "Packager": "packager",
            "Build Date": "build_date",
            "Install Date": "install_date",
            "Install Reason": "install_reason",
            "Install Script": "install_script",
            "Validated By": "validated_by",
        }

        list_keys = {"depends_on", "provides", "required_by", "optional_for", "conflicts_with", "replaces", "groups", "licenses"}
        dict_keys = {"optional_deps"}

        for line in lines:
            if ':' in line:
                key_str, value = line.split(':', 1)
                key_str = key_str.strip()
                value = value.strip()
                current_key = key_map.get(key_str)
                if current_key:
                    if current_key in list_keys:
                        # Handle "None" value
                        data[current_key] = [v.strip() for v in value.split()] if value != "None" else []
                    elif current_key in dict_keys:
                         data[current_key] = {} # Will be populated by subsequent lines
                    elif current_key == 'size':
                        size_bytes = parse_size(value)
                        data[current_key] = size_bytes if size_bytes is not None else 0
                    elif current_key == 'install_date' or current_key == 'build_date':
                        try:
                            # Example format: Wed 01 Jan 2023 12:00:00 PM UTC
                            # Need robust parsing, try common formats
                            dt = time.mktime(time.strptime(value.replace(" UTC", ""), "%a %d %b %Y %I:%M:%S %p"))
                            data[current_key] = dt
                        except ValueError:
                             logger.warning(f"Could not parse date format: {value}")
                             data[current_key] = None
                    else:
                        data[current_key] = value if value != "None" else None

            elif current_key and current_key in dict_keys and line.strip():
                 # Handle multi-line dictionary values (Optional Deps)
                 dep_line = line.strip()
                 if ':' in dep_line:
                     dep_name, dep_reason = dep_line.split(':', 1)
                     data[current_key][dep_name.strip()] = dep_reason.strip()

            elif current_key and current_key in list_keys and line.strip():
                 # Handle multi-line list values
                 data[current_key].extend([v.strip() for v in line.strip().split()])


        # Post-processing
        data['is_dependency'] = bool(data.get('required_by')) or data.get('install_reason') == 'Installed as a dependency for another package'

        return data


    def _collect_pacman_cache_info(self) -> Iterator[ScannedItem]:
        """Collects info about files in the pacman cache directory."""
        cache_dir = Path('/var/cache/pacman/pkg/')
        logger.info(f"Scanning pacman cache directory: {cache_dir}")
        if not cache_dir.is_dir():
            logger.warning(f"Pacman cache directory not found or not accessible: {cache_dir}")
            return

        exclude_patterns = self.config.get_exclude_patterns() # Respect global excludes?

        for item_path in cache_dir.glob('*.pkg.tar.*'): # Matches .zst, .xz etc.
            if item_path.is_file():
                 if is_path_excluded(item_path, exclude_patterns): continue
                 try:
                     stat_result = item_path.stat()
                     yield ScannedItem(
                         path=item_path.resolve(),
                         size_bytes=stat_result.st_size,
                         last_accessed=stat_result.st_atime,
                         last_modified=stat_result.st_mtime,
                         item_type=ITEM_TYPE_PACMAN_CACHE,
                         extra_info={} # Could add package name/version parsing here if needed
                     )
                 except OSError as e:
                     logger.warning(f"Could not process pacman cache file {item_path}: {e}")


    def _collect_journal_info(self) -> Iterator[ScannedItem]:
        """Collects info about systemd journal files."""
        # Journal files are typically in /var/log/journal/<machine-id>/
        # Getting exact size per file might require root or journalctl commands.
        # Let's represent the main journal directory or use `journalctl --disk-usage`.

        logger.info("Collecting journal log information...")
        journal_dir_persistent = Path('/var/log/journal')
        journal_dir_volatile = Path('/run/log/journal')
        journal_path = None
        total_size = 0

        # Prefer persistent logs if they exist
        if journal_dir_persistent.is_dir():
            journal_path = journal_dir_persistent
        elif journal_dir_volatile.is_dir():
            journal_path = journal_dir_volatile

        if journal_path:
            # Use journalctl --disk-usage for accurate size
            cmd = ['journalctl', '--disk-usage']
            result = run_command(cmd, capture_output=True, check=False)
            if result.returncode == 0 and result.stdout:
                # Example output: "Archived and active journals take up 1.1G on disk."
                match = re.search(r'take up\s+([\d.]+[BKMGT])\s+on disk', result.stdout)
                if match:
                    size_str = match.group(1)
                    parsed_size = parse_size(size_str)
                    if parsed_size is not None:
                        total_size = parsed_size
                        logger.info(f"Journal disk usage reported by journalctl: {size_str}")
                    else:
                         logger.warning(f"Could not parse journal disk usage size: {size_str}")
                else:
                    logger.warning("Could not parse journalctl --disk-usage output.")
            else:
                logger.warning(f"journalctl --disk-usage failed (Code: {result.returncode}): {result.stderr}. Falling back to directory scan (may be inaccurate).")
                # Fallback: estimate size by walking the directory (requires permissions)
                try:
                    total_size = sum(f.stat().st_size for f in journal_path.glob('**/*') if f.is_file())
                except OSError as e:
                    logger.warning(f"Could not scan journal directory {journal_path} for size: {e}")
                    total_size = -1 # Indicate error/unknown

            if total_size >= 0:
                 # Represent the journal as a single item pointing to the main directory
                 try:
                     stat_result = journal_path.stat() # Get dir times
                     yield ScannedItem(
                         path=journal_path.resolve(),
                         size_bytes=total_size,
                         last_accessed=stat_result.st_atime,
                         last_modified=stat_result.st_mtime,
                         item_type=ITEM_TYPE_JOURNAL_LOG,
                         extra_info={'source': 'journalctl' if match else 'scan'}
                     )
                 except OSError as e:
                     logger.warning(f"Could not stat journal directory {journal_path}: {e}")

        else:
            logger.info("No systemd journal directory found.")


    def _mark_duplicates_in_db(self, min_size: int):
        """Identifies duplicate files based on stored hashes and marks them in the DB."""
        logger.info(f"Identifying and marking duplicates (min size: {min_size} bytes)...")
        potential_dups = self.db.find_potential_duplicates(min_size)
        count = 0
        if not potential_dups:
            logger.info("No potential duplicate hashes found.")
            return

        logger.info(f"Found {len(potential_dups)} potential duplicate hashes.")
        if not self.db.conn: return

        try:
            with self.db.conn: # Use transaction
                for file_hash, size_bytes, num_files in potential_dups:
                    # Double check hash is not empty/null just in case
                    if not file_hash: continue

                    # Verify by getting paths (already done by find_potential_duplicates query)
                    # paths = self.db.get_files_by_hash(file_hash)
                    # if len(paths) > 1:
                    logger.debug(f"Marking {num_files} files with hash {file_hash[:8]}... (size: {size_bytes}) as duplicates.")
                    self.db.mark_duplicates(file_hash)
                    count += num_files
                    # else: # Should not happen based on query
                    #    logger.warning(f"Hash {file_hash} reported as duplicate but only found {len(paths)} files.")

            logger.info(f"Marked {count} files belonging to {len(potential_dups)} duplicate sets.")
        except sqlite3.Error as e:
            logger.error(f"Database error while marking duplicates: {e}", exc_info=True)
            raise # Re-raise to be caught by collect_all


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

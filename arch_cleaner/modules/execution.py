import logging
import shutil
import os
import re
import time
from pathlib import Path
from typing import List, Optional, Any, Tuple

from ..core.models import Suggestion, ActionResult, ScannedItem, PackageInfo, DuplicateSet
from ..modules.config_manager import ConfigManager
from ..db.database import DatabaseManager
from ..utils.helpers import run_command, human_readable_size, parse_size
from .recommendation import (
    SUGGESTION_OLD_FILE, SUGGESTION_LARGE_FILE, SUGGESTION_ORPHAN_PACKAGE,
    SUGGESTION_DUPLICATE_SET, SUGGESTION_PACMAN_CACHE, SUGGESTION_JOURNAL_LOG
)

logger = logging.getLogger(__name__)

try:
    trash_cli_path = shutil.which("trash-put")
    HAS_TRASH_CLI = bool(trash_cli_path)
    if HAS_TRASH_CLI:
        logger.info(f"Using trash-cli found at: {trash_cli_path}")
except Exception:
    HAS_TRASH_CLI = False
    logger.warning("trash-cli not found. Will use basic move/delete (less safe).")


class ExecutionHandler:
    """Executes approved cleanup actions safely."""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.use_trash = self.config.get('safety.use_trash', True) and HAS_TRASH_CLI

    def execute_suggestion(self, suggestion: Suggestion, dry_run: bool) -> ActionResult:
        """
        Executes a single suggestion based on its type.

        Args:
            suggestion: The Suggestion object to execute.
            dry_run: If True, simulate actions without making changes.

        Returns:
            An ActionResult object detailing the outcome.
        """
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}Executing suggestion: {suggestion.id} ({suggestion.suggestion_type}) - {suggestion.description}")

        handler_method = getattr(self, f"_handle_{suggestion.suggestion_type.lower()}", None)

        if callable(handler_method):
            try:
                return handler_method(suggestion, dry_run)
            except Exception as e:
                logger.error(f"Error executing suggestion {suggestion.id}: {e}", exc_info=True)
                return ActionResult(suggestion=suggestion, success=False, message=f"Execution error: {e}", dry_run=dry_run)
        else:
            logger.warning(f"No handler found for suggestion type: {suggestion.suggestion_type}")
            return ActionResult(suggestion=suggestion, success=False, message=f"Unsupported suggestion type: {suggestion.suggestion_type}", dry_run=dry_run)

    def _safe_delete(self, path: Path, dry_run: bool) -> Tuple[bool, str]:
        """Safely deletes a file or directory, using trash if configured."""
        target = str(path.resolve())
        if not path.exists():
            return True, f"Path {target} already gone."

        if dry_run:
            action = "Trash" if self.use_trash else "Delete"
            logger.info(f"[DRY RUN] Would {action} {target}")
            return True, f"Simulated {action.lower()} of {target}"

        try:
            if self.use_trash:
                result = run_command(['trash-put', target], capture_output=True, check=False)
                if result.returncode == 0:
                    return True, f"Moved {target} to trash."
                else:
                    logger.error(f"trash-put failed for {target}: {result.stderr}")
                    return False, f"Failed to move {target} to trash: {result.stderr}"
            else:
                logger.warning(f"Permanently deleting: {target}")
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                return True, f"Permanently deleted {target}"
        except Exception as e:
            logger.error(f"Error during safe delete of {target}: {e}", exc_info=True)
            return False, f"Error deleting {target}: {e}"

    def _handle_old_file(self, suggestion: Suggestion, dry_run: bool) -> ActionResult:
        """Handles deletion of a single old file."""
        item = suggestion.data
        if not isinstance(item, ScannedItem):
            return ActionResult(suggestion=suggestion, success=False, message="Invalid data type for OLD_FILE", dry_run=dry_run)

        success, message = self._safe_delete(item.path, dry_run)
        bytes_freed = item.size_bytes if success else 0

        if success and not dry_run:
            self.db.delete_scanned_item(item.path)

        return ActionResult(suggestion=suggestion, success=success, message=message, bytes_freed=bytes_freed, dry_run=dry_run)

    def _handle_large_file(self, suggestion: Suggestion, dry_run: bool) -> ActionResult:
        """Handles deletion of a single large file."""
        return self._handle_old_file(suggestion, dry_run)

    def _handle_orphan_package(self, suggestion: Suggestion, dry_run: bool) -> ActionResult:
        """Handles removal of orphan packages."""
        orphans = suggestion.data
        if not isinstance(orphans, list) or not all(isinstance(p, PackageInfo) for p in orphans):
             return ActionResult(suggestion=suggestion, success=False, message="Invalid data type for ORPHAN_PACKAGE", dry_run=dry_run)

        if not orphans:
            return ActionResult(suggestion=suggestion, success=True, message="No orphan packages specified.", dry_run=dry_run)

        package_names = [pkg.name for pkg in orphans]
        command = ['sudo', 'pacman', '-Rns'] + package_names

        if dry_run:
            logger.info(f"[DRY RUN] Would run command: {' '.join(command)}")
            bytes_freed = suggestion.estimated_size_bytes
            return ActionResult(suggestion=suggestion, success=True, message=f"Simulated removal of {len(package_names)} orphans.", bytes_freed=bytes_freed, dry_run=True)

        logger.info(f"Executing command: {' '.join(command)}")
        result = run_command(command, capture_output=True, check=False)

        if result.returncode == 0:
            message = f"Successfully removed {len(package_names)} orphan packages."
            bytes_freed = suggestion.estimated_size_bytes
            for pkg_name in package_names:
                self.db.delete_package(pkg_name)
            return ActionResult(suggestion=suggestion, success=True, message=message, bytes_freed=bytes_freed, dry_run=False)
        else:
            error_msg = result.stderr.strip() or f"pacman exited with code {result.returncode}"
            logger.error(f"Failed to remove orphans: {error_msg}")
            return ActionResult(suggestion=suggestion, success=False, message=f"Failed to remove orphans: {error_msg}", dry_run=False)

    def _handle_duplicate_set(self, suggestion: Suggestion, dry_run: bool) -> ActionResult:
        """Handles removal of duplicate files, keeping one copy."""
        dup_set = suggestion.data
        if not isinstance(dup_set, DuplicateSet) or len(dup_set.paths) < 2:
            return ActionResult(suggestion=suggestion, success=False, message="Invalid data for DUPLICATE_SET", dry_run=dry_run)

        paths_to_keep = [dup_set.paths[0]]
        paths_to_remove = dup_set.paths[1:]
        logger.info(f"Duplicate set {dup_set.file_hash[:8]}: Keeping {paths_to_keep[0]}, removing {len(paths_to_remove)} others.")

        total_bytes_freed = 0
        success_count = 0
        messages = []

        for path in paths_to_remove:
            success, message = self._safe_delete(path, dry_run)
            messages.append(message)
            if success:
                success_count += 1
                total_bytes_freed += dup_set.size_bytes
                if not dry_run:
                    self.db.delete_scanned_item(path)
            else:
                logger.warning(f"Failed to remove duplicate file {path}: {message}")

        overall_success = success_count == len(paths_to_remove)
        final_message = f"Removed {success_count}/{len(paths_to_remove)} duplicates. Kept: {paths_to_keep[0].name}."
        return ActionResult(suggestion=suggestion, success=overall_success, message=final_message, bytes_freed=total_bytes_freed, dry_run=dry_run)

    def _handle_pacman_cache(self, suggestion: Suggestion, dry_run: bool) -> ActionResult:
        """Handles cleaning older pacman cache files."""
        paths_to_remove = suggestion.data
        if not isinstance(paths_to_remove, list) or not all(isinstance(p, Path) for p in paths_to_remove):
             return ActionResult(suggestion=suggestion, success=False, message="Invalid data type for PACMAN_CACHE", dry_run=dry_run)

        if not paths_to_remove:
            return ActionResult(suggestion=suggestion, success=True, message="No pacman cache files to remove.", dry_run=dry_run)

        paths_str = [str(p) for p in paths_to_remove]
        command = ['sudo', 'rm', '-f'] + paths_str

        if dry_run:
            logger.info(f"[DRY RUN] Would run command: {' '.join(command)}")
            bytes_freed = suggestion.estimated_size_bytes
            return ActionResult(suggestion=suggestion, success=True, message=f"Simulated removal of {len(paths_str)} cache files.", bytes_freed=bytes_freed, dry_run=True)

        logger.info(f"Executing command: {' '.join(command)}")
        result = run_command(command, capture_output=True, check=False)

        if result.returncode == 0:
            message = f"Successfully removed {len(paths_str)} pacman cache files."
            bytes_freed = suggestion.estimated_size_bytes
            for path in paths_to_remove:
                self.db.delete_scanned_item(path)
            return ActionResult(suggestion=suggestion, success=True, message=message, bytes_freed=bytes_freed, dry_run=False)
        else:
            error_msg = result.stderr.strip() or f"sudo rm exited with code {result.returncode}"
            logger.error(f"Failed to remove pacman cache files: {error_msg}")
            return ActionResult(suggestion=suggestion, success=False, message=f"Failed to remove cache files: {error_msg}", dry_run=False)

    def _handle_journal_log(self, suggestion: Suggestion, dry_run: bool) -> ActionResult:
        """Handles vacuuming journal logs."""
        vacuum_params = suggestion.data
        if not isinstance(vacuum_params, dict):
            return ActionResult(suggestion=suggestion, success=False, message="Invalid data for JOURNAL_LOG", dry_run=dry_run)

        target_size = vacuum_params.get('target_size')
        command = ['sudo', 'journalctl']
        if target_size is not None:
            command.extend(['--vacuum-size', str(target_size)])
        else:
            return ActionResult(suggestion=suggestion, success=False, message="No target size specified for journal vacuum.", dry_run=dry_run)

        if dry_run:
            logger.info(f"[DRY RUN] Would run command: {' '.join(command)}")
            return ActionResult(suggestion=suggestion, success=True, message="Simulated journal vacuum.", bytes_freed=0, dry_run=True)

        logger.info(f"Executing command: {' '.join(command)}")
        result = run_command(command, capture_output=True, check=False)

        if result.returncode == 0:
            freed_match = re.search(r'freed\s+([\d.]+[BKMGT])', result.stdout)
            bytes_freed = parse_size(freed_match.group(1)) if freed_match else 0
            message = f"Successfully vacuumed journal logs. Freed approx {human_readable_size(bytes_freed)}."
            return ActionResult(suggestion=suggestion, success=True, message=message, bytes_freed=bytes_freed, dry_run=False)
        else:
            error_msg = result.stderr.strip() or f"journalctl exited with code {result.returncode}"
            logger.error(f"Failed to vacuum journal: {error_msg}")
            return ActionResult(suggestion=suggestion, success=False, message=f"Failed to vacuum journal: {error_msg}", dry_run=False)


# Example Usage (requires setup from previous examples)
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    temp_dir = Path("./temp_collector_test")
    config_file = temp_dir / "config.toml"
    db_file = temp_dir / "test_collector.db"

    if not config_file.exists() or not db_file.exists():
        print("Please run the previous examples (collection, analysis, recommendation) first.")
    else:
        try:
            cfg_manager = ConfigManager(config_file)
            db_manager = DatabaseManager(db_file)
            analyzer = AnalysisEngine(cfg_manager, db_manager)
            recommender = RecommendationEngine(cfg_manager)
            executor = ExecutionHandler(cfg_manager, db_manager)

            # Generate some suggestions first
            analysis_results = analyzer.analyze_all()
            suggestions = recommender.generate_suggestions(analysis_results)

            if not suggestions:
                print("No suggestions generated to execute.")
            else:
                print(f"\n--- Executing {len(suggestions)} Suggestions (Dry Run) ---")
                results_dry = []
                for sugg in suggestions:
                    print(f"\nExecuting (Dry Run): {sugg.description}")
                    result = executor.execute_suggestion(sugg, dry_run=True)
                    print(f"  Result: Success={result.success}, Msg='{result.message}', Freed={human_readable_size(result.bytes_freed)}")
                    results_dry.append(result)

                # Example: Execute the first 'OLD_FILE' suggestion for real (if exists)
                first_old_file_sugg = next((s for s in suggestions if s.suggestion_type == SUGGESTION_OLD_FILE), None)
                if first_old_file_sugg:
                    print(f"\n--- Executing Suggestion FOR REAL (Use Trash: {executor.use_trash}) ---")
                    print(f"Executing: {first_old_file_sugg.description}")
                    # Ensure the file exists before trying to delete
                    if isinstance(first_old_file_sugg.data, ScannedItem) and first_old_file_sugg.data.path.exists():
                         result_real = executor.execute_suggestion(first_old_file_sugg, dry_run=False)
                         print(f"  Result: Success={result_real.success}, Msg='{result_real.message}', Freed={human_readable_size(result_real.bytes_freed)}")
                         # Verify file is gone or in trash
                         if result_real.success:
                             print(f"  File exists after execution? {first_old_file_sugg.data.path.exists()}")
                    else:
                         print("  Skipping real execution: File does not exist or data invalid.")

                else:
                    print("\nNo OLD_FILE suggestion found to execute for real.")

        except Exception as e:
            logger.exception("Error during ExecutionHandler example")
        finally:
             if 'db_manager' in locals():
                 db_manager.close()

import logging
import json
import os
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..core.models import Suggestion, ActionResult, ActionFeedback, ScannedItem, PackageInfo, DuplicateSet
from ..modules.config_manager import ConfigManager
from ..db.database import DatabaseManager
from ..modules.collection import DataCollector
from ..modules.analysis import AnalysisEngine
from ..modules.recommendation import RecommendationEngine
from ..modules.execution import ExecutionHandler
from ..modules.learning import LearningModule
from ..utils.helpers import human_readable_size

logger = logging.getLogger(__name__)

XDG_DATA_HOME = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
APP_NAME = "arch-cleaner"
DEFAULT_SUGGESTIONS_PATH = XDG_DATA_HOME / APP_NAME / "last_suggestions.json"


class CoreController:
    """
    Orchestrates the workflow of the storage agent.
    Initializes and coordinates all the modules for data collection, analysis,
    recommendation, execution, and learning.
    """

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager, suggestions_path: Path = DEFAULT_SUGGESTIONS_PATH):
        """
        Initializes the CoreController.
        Args:
            config_manager: Manages configuration settings.
            db_manager: Manages database interactions.
            suggestions_path: Path to store the last generated suggestions.
        """
        self.config = config_manager
        self.db = db_manager
        self.suggestions_path = suggestions_path
        self.suggestions_path.parent.mkdir(parents=True, exist_ok=True)

        self.collector = DataCollector(self.config, self.db)
        self.analyzer = AnalysisEngine(self.config, self.db)
        self.recommender = RecommendationEngine(self.config)
        self.executor = ExecutionHandler(self.config, self.db)
        self.learner = LearningModule(self.config, self.db)

    def scan(self, force: bool = False, directory: Optional[str] = None) -> None:
        """
        Initiates the data collection process.
        Args:
            force: If True, forces a rescan even if recent data exists.
            directory: If provided, scan this specific directory instead of defaults.
        """
        scan_target_msg = f"directory '{directory}'" if directory else "default locations"
        logger.info(f"Scan requested for {scan_target_msg} {'(forced)' if force else ''}.")

        try:
            self.collector.collect_all(force_rescan=force, target_directory=directory)
            logger.info("Scan process completed.")
        except Exception as e:
            logger.error(f"Scan process failed: {e}", exc_info=True)

    def suggest(self, limit: Optional[int] = None) -> List[Suggestion]:
        """
        Analyzes collected data and generates cleanup suggestions.

        Args:
            limit: Maximum number of suggestions to return.

        Returns:
            A list of Suggestion objects.
        """
        logger.info("Suggestion generation requested.")
        try:
            analysis_results = self.analyzer.analyze_all()
            suggestions = self.recommender.generate_suggestions(analysis_results)
            self._save_suggestions_to_file(suggestions)

            if limit is not None and limit > 0:
                logger.debug(f"Applying suggestion limit: {limit}")
                return suggestions[:limit]
            return suggestions

        except Exception as e:
            logger.error(f"Suggestion generation failed: {e}", exc_info=True)
            self._save_suggestions_to_file([])
            return []

    def _save_suggestions_to_file(self, suggestions: List[Suggestion]):
        """Saves the list of suggestions to a JSON file."""
        def suggestion_encoder(obj):
            if isinstance(obj, Path):
                return str(obj)
            if hasattr(obj, '__dict__'):
                d = obj.__dict__.copy()
                for k, v in d.items():
                    if isinstance(v, Path):
                        d[k] = str(v)
                    elif isinstance(v, list) and v and isinstance(v[0], Path):
                        d[k] = [str(p) for p in v]
                return d
            raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

        try:
            self.suggestions_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.suggestions_path, 'w') as f:
                json.dump([s.__dict__ for s in suggestions], f, default=suggestion_encoder, indent=2)
            logger.info(f"Saved {len(suggestions)} suggestions to {self.suggestions_path}")
        except (TypeError, IOError) as e:
            logger.error(f"Failed to save suggestions to {self.suggestions_path}: {e}", exc_info=True)

    def apply(self, suggestion_ids: Optional[List[str]] = None, dry_run: bool = False, auto_approve: bool = False) -> List[ActionResult]:
        """
        Applies selected suggestions or all last generated suggestions.

        Args:
            suggestion_ids: A list of specific suggestion IDs to apply. If None, applies all from last 'suggest' call.
            dry_run: If True, simulate actions without making changes.
            auto_approve: If True, skips confirmation (used internally by 'auto' command, CLI handles interactive approval).

        Returns:
            A list of ActionResult objects for each applied suggestion.
        """
        logger.info(f"Apply requested {'(Dry Run)' if dry_run else ''}.")
        results: List[ActionResult] = []
        suggestions_from_file = self.get_last_suggestions()

        if not suggestions_from_file:
            logger.warning("No suggestions available to apply. Run 'suggest' first.")
            return results

        suggestions_to_apply = suggestions_from_file
        if suggestion_ids:
            suggestion_map = {s.id: s for s in suggestions_from_file}
            suggestions_to_apply = [suggestion_map[s_id] for s_id in suggestion_ids if s_id in suggestion_map]

        if not suggestions_to_apply:
            logger.warning("No matching suggestions found to apply.")
            return results

        logger.info(f"Applying {len(suggestions_to_apply)} suggestions...")
        total_bytes_freed = 0

        for suggestion in suggestions_to_apply:
            logger.debug(f"Processing suggestion: {suggestion.id} - {suggestion.description}")
            action_result = self.executor.execute_suggestion(suggestion, dry_run)
            results.append(action_result)

            if not dry_run:
                action_taken = 'APPROVED' if action_result.success else 'EXECUTION_FAILED'
                self.learner.record_feedback(suggestion, action_taken=action_taken)
                if action_result.success:
                    total_bytes_freed += action_result.bytes_freed

        if not dry_run:
            logger.info(f"Apply process finished. Total estimated bytes freed: {total_bytes_freed} ({human_readable_size(total_bytes_freed)})")
        else:
            logger.info("Dry run finished.")

        return results

    def run_auto(self, dry_run: bool = False) -> List[ActionResult]:
        """
        Runs the full scan -> suggest -> apply cycle based on automation rules.
        """
        logger.info(f"Auto mode requested {'(Dry Run)' if dry_run else ''}.")
        self.scan()
        suggestions = self.suggest()
        if not suggestions:
            logger.info("Auto mode: No suggestions generated.")
            return []

        min_confidence = self.config.get('automation.min_confidence', 0.8)
        suggestions_to_apply = [s for s in suggestions if s.confidence >= min_confidence]
        logger.info(f"Auto mode: Applying {len(suggestions_to_apply)} suggestions with confidence >= {min_confidence}")

        if not suggestions_to_apply:
            logger.info("Auto mode: No suggestions met the confidence threshold.")
            return []

        suggestion_ids_to_apply = [s.id for s in suggestions_to_apply]
        results = self.apply(suggestion_ids=suggestion_ids_to_apply, dry_run=dry_run, auto_approve=True)
        logger.info("Auto mode finished.")
        return results

    def get_status(self) -> Dict[str, Any]:
        """Retrieves current status information."""
        logger.debug("Status requested.")
        status = {}
        try:
            status['last_scan_time'] = self.db.get_last_scan_time()
            db_size = -1
            if self.db.db_path.exists():
                try:
                    db_size = self.db.db_path.stat().st_size
                except OSError:
                    pass
            status['database_size_bytes'] = db_size
            status['config_path'] = str(self.config.config_path)
            status['database_path'] = str(self.db.db_path)
        except Exception as e:
            logger.error(f"Error retrieving status: {e}", exc_info=True)
            status['error'] = str(e)
        return status

    def generate_report(self) -> Dict[str, Any]:
        """Generates a report of recent actions and total savings."""
        logger.debug("Report requested.")
        report = {}
        try:
            feedback_limit = 20
            report['recent_actions'] = self.db.get_feedback(limit=feedback_limit)
            report['total_saved_estimate'] = "Not implemented"
        except Exception as e:
            logger.error(f"Error generating report: {e}", exc_info=True)
            report['error'] = str(e)
        return report

    def get_last_suggestions(self) -> List[Suggestion]:
        """Loads and returns the list of suggestions from the last 'suggest' call."""
        if not self.suggestions_path.exists():
            logger.info(f"Suggestions file not found: {self.suggestions_path}")
            return []

        suggestions: List[Suggestion] = []
        try:
            with open(self.suggestions_path, 'r') as f:
                suggestions_data = json.load(f)

            for sugg_data in suggestions_data:
                try:
                    reconstructed_data = self._reconstruct_suggestion_data(sugg_data)
                    suggestion_obj = Suggestion(
                        id=sugg_data.get('id', 'unknown'),
                        suggestion_type=sugg_data.get('suggestion_type') or 'unknown',
                        description=sugg_data.get('description', ''),
                        details=sugg_data.get('details'),
                        estimated_size_bytes=sugg_data.get('estimated_size_bytes', 0),
                        confidence=sugg_data.get('confidence', 0.0),
                        data=reconstructed_data
                    )
                    suggestions.append(suggestion_obj)
                except (TypeError, KeyError, ValueError) as e:
                    logger.warning(f"Skipping suggestion due to deserialization error: {e}. Suggestion ID: {sugg_data.get('id', 'N/A')}", exc_info=False)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load suggestions from {self.suggestions_path}: {e}", exc_info=True)
            return []

        return suggestions

    def _reconstruct_suggestion_data(self, sugg_data: Dict[str, Any]) -> Optional[Any]:
        """Helper to reconstruct nested data objects from suggestion data."""
        data_field = sugg_data.get('data')
        suggestion_type = sugg_data.get('suggestion_type')

        if not isinstance(data_field, dict):
            return None

        if suggestion_type in ['OLD_FILE', 'LARGE_FILE', 'CACHE_FILE', 'LOG_FILE', 'PACMAN_CACHE_FILE', 'JOURNAL_LOG']:
            path_str = data_field.get('path')
            return ScannedItem(
                path=Path(path_str) if path_str else Path('.'),
                size_bytes=data_field.get('size_bytes', 0),
                last_accessed=data_field.get('last_accessed', 0),
                last_modified=data_field.get('last_modified', 0),
                item_type=data_field.get('item_type', 'unknown'),
                extra_info=data_field.get('extra_info', {})
            )
        elif suggestion_type == 'ORPHAN_PACKAGE':
            return PackageInfo(**data_field)
        elif suggestion_type == 'DUPLICATE_FILES':
            paths_str = data_field.get('paths', [])
            return DuplicateSet(
                file_hash=data_field.get('file_hash', 'unknown'),
                size_bytes=data_field.get('size_bytes', 0),
                paths=[Path(p) for p in paths_str] if paths_str else []
            )
        return None

    def record_manual_feedback(self, suggestion_id: str, action: str):
        """Records user feedback for a suggestion (e.g., 'SKIPPED')."""
        last_suggestions = self.get_last_suggestions()
        suggestion = next((s for s in last_suggestions if s.id == suggestion_id), None)
        if suggestion:
            self.learner.record_feedback(suggestion, action_taken=action.upper())
            logger.info(f"Manually recorded feedback for suggestion {suggestion_id}: {action}")
        else:
            logger.warning(f"Cannot record manual feedback: Suggestion ID {suggestion_id} not found.")

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    temp_dir = Path("./temp_collector_test")
    config_file = temp_dir / "config.toml"
    db_file = temp_dir / "test_collector.db"

    if not config_file.exists() or not db_file.exists():
        print("Please run the previous examples first to create test data.")
    else:
        try:
            cfg_manager = ConfigManager(config_file)
            db_manager = DatabaseManager(db_file)
            controller = CoreController(cfg_manager, db_manager)

            print("\n--- Running Scan ---")
            controller.scan()

            print("\n--- Generating Suggestions ---")
            suggestions = controller.suggest(limit=10)
            if suggestions:
                print(f"Generated {len(suggestions)} suggestions:")
                for i, sugg in enumerate(suggestions):
                    print(f"  {i+1}. [{sugg.id}] {sugg.description} ({human_readable_size(sugg.estimated_size_bytes)})")
            else:
                print("No suggestions generated.")

            # Example: Apply first suggestion (if any) in dry run
            if suggestions:
                print("\n--- Applying First Suggestion (Dry Run) ---")
                results_dry = controller.apply(suggestion_ids=[suggestions[0].id], dry_run=True)
                if results_dry:
                    res = results_dry[0]
                    print(f"  Result: Success={res.success}, Msg='{res.message}'")

            print("\n--- Getting Status ---")
            status = controller.get_status()
            print(f"  Status: {status}")

            print("\n--- Generating Report ---")
            report = controller.generate_report()
            print(f"  Recent Actions ({len(report.get('recent_actions',[]))}):")
            for action in report.get('recent_actions', [])[:3]: # Show first 3
                 print(f"    - {action.suggestion_type} ({action.item_details}) -> {action.action_taken}")


        except Exception as e:
            logger.exception("Error during CoreController example")
        finally:
             if 'db_manager' in locals():
                 db_manager.close()

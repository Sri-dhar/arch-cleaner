import logging
import time
from typing import Optional, Any # Added Any

from ..core.models import Suggestion, ActionFeedback, ActionResult
from ..db.database import DatabaseManager
from ..modules.config_manager import ConfigManager

logger = logging.getLogger(__name__)

class LearningModule:
    """Handles learning from user feedback to adapt suggestions."""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.learning_enabled = self.config.get('learning.enabled', True)
        self.feedback_limit = self.config.get('learning.feedback_history_limit', 1000) # TODO: Implement pruning

        if not self.learning_enabled:
            logger.info("Learning module is disabled in configuration.")

    def record_feedback(self, suggestion: Suggestion, action_taken: str, user_comment: Optional[str] = None):
        """
        Records user feedback about a specific suggestion into the database.

        Args:
            suggestion: The Suggestion object the feedback pertains to.
            action_taken: The action performed by the user (e.g., 'APPROVED', 'REJECTED', 'SKIPPED').
            user_comment: Optional comment from the user.
        """
        if not self.learning_enabled:
            return

        # Extract relevant details for storage (avoid storing large data objects)
        item_details = suggestion.details # Use the pre-formatted details string

        feedback = ActionFeedback(
            suggestion_id=suggestion.id,
            # suggestion_type=suggestion.suggestion_type, # Removed incorrect argument
            # item_details=item_details, # Removed incorrect argument
            action_taken=action_taken,
            timestamp=time.time(), # Use current time for feedback
            user_comment=user_comment
        )

        try:
            self.db.add_feedback(feedback)
            logger.debug(f"Recorded feedback: Suggestion {suggestion.id}, Action {action_taken}")
            # TODO: Prune old feedback if limit is configured and > 0
        except Exception as e:
            logger.error(f"Failed to record feedback for suggestion {suggestion.id}: {e}", exc_info=True)

    def adapt_rules(self):
        """
        (Placeholder) Adapts internal rules or thresholds based on stored feedback.
        This could involve analyzing patterns in rejected/approved suggestions.
        """
        if not self.learning_enabled:
            return
        logger.info("Placeholder: Adapting rules based on feedback (not implemented).")
        # Example: Fetch feedback, analyze rejection patterns for certain file types/ages,
        # adjust thresholds in config or internal state.
        # feedback_data = self.db.get_feedback(limit=self.feedback_limit)
        # ... analysis logic ...

    def train_model(self):
        """
        (Placeholder) Trains or retrains an ML model based on stored feedback.
        Requires ML libraries (e.g., scikit-learn) and feature engineering.
        """
        if not self.learning_enabled:
            return
        logger.info("Placeholder: Training ML model based on feedback (not implemented).")

    def get_confidence_adjustment(self, suggestion_type: str, item_details: Any) -> float:
        """
        (Placeholder) Returns an adjustment factor for suggestion confidence based on learned data.
        Could use simple rules or ML model predictions.
        """
        if not self.learning_enabled:
            return 1.0 # No adjustment if disabled

        # TODO: Implement logic based on feedback analysis or ML model prediction
        return 1.0

# Example Usage
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Use artifacts from previous examples
    temp_dir = Path("./temp_collector_test")
    config_file = temp_dir / "config.toml"
    db_file = temp_dir / "test_collector.db"

    if not config_file.exists() or not db_file.exists():
        print("Please run the previous examples first to create test data.")
    else:
        try:
            cfg_manager = ConfigManager(config_file)
            # Ensure learning is enabled in config for test
            cfg_manager.config['learning']['enabled'] = True
            cfg_manager.learning_enabled = True

            db_manager = DatabaseManager(db_file)
            learner = LearningModule(cfg_manager, db_manager)

            print("\n--- Recording Feedback ---")
            # Create dummy suggestions to record feedback for
            sugg1 = Suggestion(id='sugg_old1', suggestion_type='OLD_FILE', description='Old file', details='/tmp/old.txt', estimated_size_bytes=100)
            sugg2 = Suggestion(id='sugg_dup1', suggestion_type='DUPLICATE_SET', description='Duplicates', details='hash123...', estimated_size_bytes=2000)

            learner.record_feedback(sugg1, action_taken='APPROVED')
            learner.record_feedback(sugg2, action_taken='REJECTED', user_comment="Need this file")

            print("\n--- Retrieving Feedback ---")
            feedback_list = db_manager.get_feedback(limit=10)
            if feedback_list:
                for fb in feedback_list:
                    print(f"- ID: {fb.suggestion_id}, Type: {fb.suggestion_type}, Action: {fb.action_taken}, Details: {fb.item_details}, Comment: {fb.user_comment}")
            else:
                print("No feedback found in DB.")

            print("\n--- Placeholder Actions ---")
            learner.adapt_rules()
            learner.train_model()
            adj = learner.get_confidence_adjustment('OLD_FILE', '/tmp/another_old.txt')
            print(f"Confidence adjustment factor: {adj}")


        except Exception as e:
            logger.exception("Error during LearningModule example")
        finally:
             if 'db_manager' in locals():
                 db_manager.close()

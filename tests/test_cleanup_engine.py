from unittest.mock import MagicMock

from tests.conftest import make_email

from src.cleanup_engine import execute_cleanup
from src.config import Action, Category
from src.models import ClassificationResult, RecommendedAction


def _rec(message_id, category, action, protected=False, days_old=45):
    email = make_email(message_id=message_id, days_old=days_old)
    classification = ClassificationResult(
        message_id=message_id, category=category, confidence=0.95,
        reason="test", source="rule",
    )
    return RecommendedAction(
        message_id=message_id, email=email, classification=classification,
        recommended_action=action, protected=protected,
    )


def test_dry_run_performs_no_gmail_mutation(config, tmp_path):
    config.dry_run = True
    gmail = MagicMock()
    audit = MagicMock()
    recs = [_rec("m1", Category.PROMOTIONAL, Action.ARCHIVE),
            _rec("m2", Category.SPAM, Action.TRASH)]

    summary = execute_cleanup(gmail, recs, config, audit, trash_confirmed=True)

    gmail.archive_batch.assert_not_called()
    gmail.trash_batch.assert_not_called()
    assert summary.skipped == 2
    assert summary.archived == 0
    assert summary.trashed == 0


def test_trash_without_confirmation_is_skipped(config):
    config.dry_run = False
    gmail = MagicMock()
    gmail.trash_batch.return_value = []
    audit = MagicMock()
    recs = [_rec("m1", Category.SPAM, Action.TRASH)]

    summary = execute_cleanup(gmail, recs, config, audit, trash_confirmed=False)

    gmail.trash_batch.assert_not_called()
    assert summary.trashed == 0
    assert summary.skipped == 1


def test_trash_with_confirmation_executes(config):
    config.dry_run = False
    gmail = MagicMock()
    gmail.trash_batch.return_value = []  # no failures
    audit = MagicMock()
    recs = [_rec("m1", Category.SPAM, Action.TRASH)]

    summary = execute_cleanup(gmail, recs, config, audit, trash_confirmed=True)

    gmail.trash_batch.assert_called_once_with(["m1"])
    assert summary.trashed == 1


def test_protected_emails_are_kept_and_counted(config):
    config.dry_run = False
    gmail = MagicMock()
    audit = MagicMock()
    recs = [_rec("m1", Category.FINANCE, Action.KEEP, protected=True)]

    summary = execute_cleanup(gmail, recs, config, audit, trash_confirmed=True)

    assert summary.protected == 1
    assert summary.skipped == 1
    gmail.archive_batch.assert_not_called()
    gmail.trash_batch.assert_not_called()


def test_batch_failure_is_recorded_as_error(config):
    config.dry_run = False
    gmail = MagicMock()
    gmail.archive_batch.return_value = ["m1"]  # m1 failed
    audit = MagicMock()
    recs = [_rec("m1", Category.PROMOTIONAL, Action.ARCHIVE)]

    summary = execute_cleanup(gmail, recs, config, audit, trash_confirmed=True)

    assert summary.archived == 0
    assert summary.errors == 1


def test_review_action_counted_as_manual_review(config):
    config.dry_run = False
    gmail = MagicMock()
    audit = MagicMock()
    recs = [_rec("m1", Category.UNCERTAIN, Action.REVIEW)]

    summary = execute_cleanup(gmail, recs, config, audit, trash_confirmed=True)

    assert summary.manual_review == 1
    gmail.archive_batch.assert_not_called()
    gmail.trash_batch.assert_not_called()

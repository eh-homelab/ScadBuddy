"""What a run's result says of the path it took (#3818): the dialog reads it, never a guess."""

from __future__ import annotations

from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.print_run import PrintRunResult, _queued


def test_a_queued_run_reports_the_bambuddy_path() -> None:
    outcome = QueueOutcome(sliced_library_file_id=2, queue_item_ids=[7], printer_id=1)
    result = _queued(None, [outcome], 1, None, None, 1, [])
    assert result.path == "bambuddy"
    assert result.model_dump()["path"] == "bambuddy"


def test_a_result_recorded_before_the_path_reads_as_bambuddys() -> None:
    # A stored run's result, from before #3818 carried the path.
    stored = {"library_file_id": 1, "copies": 1, "queue_item_ids": [7]}
    assert PrintRunResult.model_validate(stored).path == "bambuddy"

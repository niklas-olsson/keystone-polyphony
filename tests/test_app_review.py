import os
import json
import pytest
import asyncio
from unittest.mock import patch, mock_open

# We have to import the scripts logic dynamically since it's not a proper module
import importlib.util

spec = importlib.util.spec_from_file_location("app_review", "scripts/app_review.py")
app_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_review)


@pytest.fixture
def mock_files():
    files_content = {
        "TODO.md": "Fix bug A",
        "meta/DISCOVERIES.md": "Found a cool trick",
    }

    def side_effect(filepath, *args, **kwargs):
        if filepath in files_content:
            return mock_open(read_data=files_content[filepath]).return_value
        raise FileNotFoundError(f"No mock for {filepath}")

    return side_effect


@pytest.mark.asyncio
async def test_data_aggregation(mock_files):
    fake_swarm_status = "--- SWARM BACKLOG (0 tasks) ---"
    fake_pending = "No pending issues."

    with patch("builtins.open", new_callable=lambda: mock_files), patch.object(
        app_review, "run_script_output", return_value=fake_swarm_status
    ), patch.object(app_review, "get_pending_issues", return_value=fake_pending):
        metrics = await app_review.aggregate_data()

    assert metrics["TODO"] == "Fix bug A"
    assert metrics["DISCOVERIES"] == "Found a cool trick"
    assert metrics["swarm_status"] == fake_swarm_status


@pytest.mark.asyncio
async def test_report_generation():
    import sys

    sys.modules["scripts.app_review"] = app_review

    fake_metrics = {"TODO": "Testing", "DISCOVERIES": "Testing", "swarm_status": ""}
    fake_markdown = "# Test App Review Report\n\nAll good."

    # Because 'scripts' is not a real package, patching "scripts.app_review.aggregate_data" fails.
    # We should patch the object directly.
    with patch.object(app_review, "aggregate_data", return_value=fake_metrics), patch(
        "liminal_bridge.architect.Architect.is_configured", new_callable=lambda: True
    ), patch(
        "liminal_bridge.architect.Architect.review_app",
        return_value=fake_markdown,
    ) as mock_review, patch(
        "builtins.open", mock_open()
    ) as m_open, patch(
        "os.makedirs"
    ) as m_makedirs:

        await app_review.main()

        # Check Architect was called with metrics
        mock_review.assert_called_once_with(fake_metrics)

        # Check os.makedirs was called for docs/reviews
        m_makedirs.assert_called_once_with("docs/reviews", exist_ok=True)

        # Check file was written
        # Since mock_open is used, checking what's written requires inspecting the mocked file handle
        handle = m_open()
        handle.write.assert_called_with(fake_markdown)

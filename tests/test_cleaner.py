"""Tests for assure_package_cleaner.cleaner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from assure_package_cleaner.cleaner import Cleaner, CycleStats, _extract_timestamp, _parse_timestamp
from assure_package_cleaner.client import APIError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A timestamp guaranteed to be "stale" (very old) relative to any reasonable threshold.
_OLD_TIMESTAMP = "2020-01-01T00:00:00Z"
# A timestamp guaranteed to be "fresh" (recent).
_FRESH_TIMESTAMP = (datetime.now(UTC) - timedelta(hours=1)).isoformat()


def _make_cleaner(
    client: MagicMock | None = None,
    stale_threshold_days: int = 30,
    dry_run: bool = True,
) -> Cleaner:
    if client is None:
        client = MagicMock()
    return Cleaner(client=client, stale_threshold_days=stale_threshold_days, dry_run=dry_run)


def _status_response(timestamp: str) -> dict:
    return {"analysis": {"timestamp": timestamp}}


def _status_response_no_timestamp() -> dict:
    return {"analysis": {}}


def _status_response_no_analysis() -> dict:
    return {}


# ---------------------------------------------------------------------------
# All stale versions -> package deleted
# ---------------------------------------------------------------------------


class TestAllStaleVersions:
    def test_dry_run_logs_would_delete(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0.0"}, {"version": "2.0.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=True)
        stats = cleaner.run_cycle()

        assert stats.deleted == 1
        assert stats.skipped == 0
        assert stats.errors == 0
        # In dry-run mode, delete_package should NOT be called
        client.delete_package.assert_not_called()

    def test_actual_delete_when_not_dry_run(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 1
        client.delete_package.assert_called_once_with("grp", "proj", "pkg")


# ---------------------------------------------------------------------------
# One fresh version -> package skipped (short-circuit)
# ---------------------------------------------------------------------------


class TestFreshVersionSkip:
    def test_one_fresh_version_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [
            {"version": "1.0.0"},
            {"version": "2.0.0"},
        ]
        # First version is stale, second is fresh
        client.get_version_status.side_effect = [
            _status_response(_OLD_TIMESTAMP),
            _status_response(_FRESH_TIMESTAMP),
        ]

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.skipped == 1
        assert stats.deleted == 0
        client.delete_package.assert_not_called()

    def test_first_fresh_version_short_circuits(self):
        """When the first version is fresh, don't even check subsequent versions."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [
            {"version": "1.0.0"},
            {"version": "2.0.0"},
            {"version": "3.0.0"},
        ]
        client.get_version_status.return_value = _status_response(_FRESH_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=True)
        stats = cleaner.run_cycle()

        assert stats.skipped == 1
        assert stats.deleted == 0
        # Only one status call because it short-circuits on first fresh version
        assert client.get_version_status.call_count == 1


# ---------------------------------------------------------------------------
# API error on version status -> fail-safe skip
# ---------------------------------------------------------------------------


class TestVersionStatusError:
    def test_api_error_on_status_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0.0"}]
        client.get_version_status.side_effect = APIError("GET", "url", 500, "err")

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.deleted == 0
        client.delete_package.assert_not_called()

    def test_api_error_on_second_version_skips_whole_package(self):
        """If status fails for any version, the entire package is skipped (fail-safe)."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [
            {"version": "1.0.0"},
            {"version": "2.0.0"},
        ]
        client.get_version_status.side_effect = [
            _status_response(_OLD_TIMESTAMP),
            APIError("GET", "url", 500, "err"),
        ]

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.deleted == 0
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# API error on group listing -> handled gracefully
# ---------------------------------------------------------------------------


class TestGroupListingError:
    def test_api_error_on_list_groups_aborts_cycle(self):
        client = MagicMock()
        client.list_groups.side_effect = APIError("GET", "url", 500, "err")

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.groups_processed == 0
        assert stats.deleted == 0

    def test_api_error_on_list_projects_skips_group(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        # First group fails, second succeeds with no projects
        client.list_projects.side_effect = [
            APIError("GET", "url", 500, "err"),
            [],
        ]

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.groups_processed == 2

    def test_api_error_on_list_packages_skips_project(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.side_effect = APIError("GET", "url", 500, "err")

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.projects_processed == 1
        assert stats.packages_evaluated == 0

    def test_api_error_on_list_versions_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.side_effect = APIError("GET", "url", 500, "err")

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.packages_evaluated == 1
        assert stats.deleted == 0


# ---------------------------------------------------------------------------
# Empty versions list -> package skipped
# ---------------------------------------------------------------------------


class TestEmptyVersions:
    def test_empty_versions_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.skipped == 1
        assert stats.deleted == 0
        assert stats.packages_evaluated == 1
        client.get_version_status.assert_not_called()


# ---------------------------------------------------------------------------
# Stats accumulation
# ---------------------------------------------------------------------------


class TestStatsAccumulation:
    def test_multiple_groups_and_packages(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.side_effect = [
            [{"name": "proj-a"}],
            [{"name": "proj-b"}],
        ]
        client.list_packages.side_effect = [
            [{"name": "pkg-1"}, {"name": "pkg-2"}],
            [{"name": "pkg-3"}],
        ]
        # pkg-1: stale -> delete
        # pkg-2: fresh -> skip
        # pkg-3: stale -> delete
        client.list_versions.side_effect = [
            [{"version": "1.0"}],
            [{"version": "1.0"}],
            [{"version": "1.0"}],
        ]
        client.get_version_status.side_effect = [
            _status_response(_OLD_TIMESTAMP),
            _status_response(_FRESH_TIMESTAMP),
            _status_response(_OLD_TIMESTAMP),
        ]

        cleaner = _make_cleaner(client=client, dry_run=True)
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 2
        assert stats.projects_processed == 2
        assert stats.packages_evaluated == 3
        assert stats.deleted == 2
        assert stats.skipped == 1
        assert stats.errors == 0

    def test_delete_failure_increments_errors(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)
        client.delete_package.side_effect = APIError("DELETE", "url", 500, "err")

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.deleted == 0


# ---------------------------------------------------------------------------
# Dry run mode
# ---------------------------------------------------------------------------


class TestDryRunMode:
    def test_dry_run_does_not_call_delete(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg-a"}, {"name": "pkg-b"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=True)
        stats = cleaner.run_cycle()

        assert stats.deleted == 2
        client.delete_package.assert_not_called()

    def test_non_dry_run_calls_delete(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 1
        client.delete_package.assert_called_once_with("grp", "proj", "pkg")


# ---------------------------------------------------------------------------
# Missing timestamp -> fail-safe skip
# ---------------------------------------------------------------------------


class TestMissingTimestamp:
    def test_no_analysis_key_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response_no_analysis()

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.skipped == 1
        assert stats.deleted == 0

    def test_no_timestamp_in_analysis_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response_no_timestamp()

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.skipped == 1
        assert stats.deleted == 0


# ---------------------------------------------------------------------------
# _extract_timestamp helper
# ---------------------------------------------------------------------------


class TestExtractTimestamp:
    def test_valid_timestamp(self):
        status = {"analysis": {"timestamp": "2025-06-01T12:00:00Z"}}
        assert _extract_timestamp(status) == "2025-06-01T12:00:00Z"

    def test_no_analysis_key(self):
        assert _extract_timestamp({}) is None

    def test_analysis_is_not_dict(self):
        assert _extract_timestamp({"analysis": "string"}) is None

    def test_no_timestamp_key(self):
        assert _extract_timestamp({"analysis": {}}) is None

    def test_timestamp_is_not_string(self):
        assert _extract_timestamp({"analysis": {"timestamp": 12345}}) is None


# ---------------------------------------------------------------------------
# _parse_timestamp helper
# ---------------------------------------------------------------------------


class TestParseTimestamp:
    def test_iso_with_utc(self):
        dt = _parse_timestamp("2025-06-01T12:00:00+00:00")
        assert dt.tzinfo is not None
        assert dt.year == 2025
        assert dt.month == 6

    def test_iso_without_timezone_assumes_utc(self):
        dt = _parse_timestamp("2025-06-01T12:00:00")
        assert dt.tzinfo == UTC

    def test_iso_with_z_suffix(self):
        dt = _parse_timestamp("2025-06-01T12:00:00Z")
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# CycleStats defaults
# ---------------------------------------------------------------------------


class TestCycleStats:
    def test_defaults_are_zero(self):
        stats = CycleStats()
        assert stats.deleted == 0
        assert stats.skipped == 0
        assert stats.errors == 0
        assert stats.groups_processed == 0
        assert stats.projects_processed == 0
        assert stats.packages_evaluated == 0

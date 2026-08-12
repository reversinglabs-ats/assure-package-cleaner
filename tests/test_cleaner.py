"""Tests for assure_package_cleaner.cleaner."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

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
    client: MagicMock | _StatefulPortal | None = None,
    stale_threshold_days: int = 30,
    dry_run: bool = True,
    shutdown: threading.Event | None = None,
    target_groups: frozenset[str] = frozenset(),
    target_projects: frozenset[str] = frozenset(),
) -> Cleaner:
    if client is None:
        client = MagicMock()
    kwargs: dict = {
        "client": client,
        "stale_threshold_days": stale_threshold_days,
        "dry_run": dry_run,
        "target_groups": target_groups,
        "target_projects": target_projects,
    }
    if shutdown is not None:
        kwargs["shutdown"] = shutdown
    return Cleaner(**kwargs)


def _status_response(timestamp: str) -> dict:
    return {"analysis": {"timestamp": timestamp}}


class _StatefulPortal:
    """A fake portal where a deleted package actually disappears.

    A MagicMock's return_value hands back the same listing forever. That models dry-run
    faithfully, but under DRY_RUN=false it hides what a second visit to an already-deleted
    package really costs — the 404 lands at list_versions, before DELETE is ever reached.
    Every version it reports is stale, so anything reachable is deletable.
    """

    def __init__(
        self,
        tree: dict[str, dict[str, dict[str, list[str]]]],
        *,
        duplicate_packages: bool = False,
    ) -> None:
        self.tree = tree
        self.duplicate_packages = duplicate_packages
        self.delete_calls: list[tuple[str, str, str]] = []

    def _packages(self, group: str, project: str) -> dict[str, list[str]]:
        try:
            return self.tree[group][project]
        except KeyError:
            raise APIError("GET", f"/list/{group}/{project}", 404, "not found") from None

    def list_groups(self) -> list[dict]:
        return [{"name": name} for name in self.tree]

    def list_projects(self, group: str) -> list[dict]:
        return [{"name": name} for name in self.tree[group]]

    def list_packages(self, group: str, project: str) -> list[dict]:
        repeats = 2 if self.duplicate_packages else 1
        return [{"name": name} for name in self._packages(group, project) for _ in range(repeats)]

    def list_versions(self, group: str, project: str, package: str) -> list[dict]:
        packages = self._packages(group, project)
        if package not in packages:
            raise APIError("GET", f"/list/{group}/{project}/{package}", 404, "not found")
        return [{"version": version} for version in packages[package]]

    def get_version_status(self, group: str, project: str, package: str, version: str) -> dict:
        if package not in self._packages(group, project):
            raise APIError("GET", f"/status/{group}/{project}/{package}", 404, "not found")
        return _status_response(_OLD_TIMESTAMP)

    def delete_package(self, group: str, project: str, package: str) -> None:
        self.delete_calls.append((group, project, package))
        packages = self._packages(group, project)
        if package not in packages:
            raise APIError("DELETE", f"/delete/{group}/{project}/{package}", 404, "not found")
        del packages[package]


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

    def test_analysis_is_none(self):
        assert _extract_timestamp({"analysis": None}) is None

    def test_no_timestamp_key(self):
        assert _extract_timestamp({"analysis": {}}) is None

    def test_timestamp_is_not_string(self):
        assert _extract_timestamp({"analysis": {"timestamp": 12345}}) is None

    def test_timestamp_is_list(self):
        assert _extract_timestamp({"analysis": {"timestamp": ["2025-01-01"]}}) is None

    def test_timestamp_is_none(self):
        assert _extract_timestamp({"analysis": {"timestamp": None}}) is None

    @pytest.mark.parametrize("payload", [None, [], "x", 7, {"analysis": []}])
    def test_non_dict_payload_returns_none_instead_of_raising(self, payload):
        """The container is guarded, not just the values inside it. `_get` returns whatever
        the endpoint decoded to, so a JSON body of `null`, `[]` or `"x"` reaches here — and
        an AttributeError would abort the cycle mid-walk, after earlier deletes."""
        assert _extract_timestamp(payload) is None


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


# ---------------------------------------------------------------------------
# Boundary: timestamp exactly at cutoff
# ---------------------------------------------------------------------------


class TestBoundaryTimestamp:
    def test_timestamp_exactly_at_cutoff_is_not_stale(self):
        """A version analyzed at exactly the cutoff time should be treated as fresh."""
        stale_days = 30
        frozen_now = datetime.now(UTC)
        cutoff = frozen_now - timedelta(days=stale_days)
        exact_timestamp = cutoff.isoformat()

        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(exact_timestamp)

        cleaner = _make_cleaner(client=client, stale_threshold_days=stale_days, dry_run=False)
        with patch("assure_package_cleaner.cleaner.datetime") as mock_dt:
            mock_dt.now.return_value = frozen_now
            mock_dt.fromisoformat = datetime.fromisoformat
            stats = cleaner.run_cycle()

        assert stats.deleted == 0
        assert stats.skipped == 1
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# Missing timestamp on second version after first is stale -> fail-safe skip
# ---------------------------------------------------------------------------


class TestMissingTimestampSecondVersion:
    def test_stale_first_missing_second_skips_package(self):
        """If the first version is stale but the second has no timestamp, skip the whole package."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}, {"version": "2.0"}]
        client.get_version_status.side_effect = [
            _status_response(_OLD_TIMESTAMP),
            _status_response_no_analysis(),
        ]

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 0
        assert stats.skipped == 1
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed timestamp -> fail-safe skip (not crash)
# ---------------------------------------------------------------------------


class TestMalformedTimestamp:
    def test_unparseable_timestamp_skips_package(self):
        """A non-ISO timestamp string should skip the package, not crash the cycle."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = {"analysis": {"timestamp": "not-a-date"}}

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 0
        assert stats.errors == 1
        client.delete_package.assert_not_called()

    def test_empty_string_timestamp_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = {"analysis": {"timestamp": ""}}

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 0
        assert stats.errors == 1
        client.delete_package.assert_not_called()

    def test_malformed_timestamp_on_second_version_after_stale_first(self):
        """Malformed second timestamp after stale first -> fail-safe, no deletion."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}, {"version": "2.0"}]
        client.get_version_status.side_effect = [
            _status_response(_OLD_TIMESTAMP),
            {"analysis": {"timestamp": "pending"}},
        ]

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 0
        assert stats.errors == 1
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# Missing "name"/"version" keys in API responses -> graceful handling
# ---------------------------------------------------------------------------


class TestMissingKeys:
    def test_group_missing_name_key_skipped(self):
        client = MagicMock()
        client.list_groups.return_value = [{"id": "123"}]

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.groups_processed == 0
        assert stats.deleted == 0

    def test_project_missing_name_key_skipped(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"id": "123"}]

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.projects_processed == 0
        assert stats.deleted == 0

    def test_package_missing_name_key_skipped(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"id": "123"}]

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.packages_evaluated == 0
        assert stats.deleted == 0

    def test_version_missing_version_key_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"id": "123"}]

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.deleted == 0
        client.delete_package.assert_not_called()

    def test_non_dict_group_entry_skipped_not_fatal(self):
        """A non-dict group entry must be skipped, not crash the cycle mid-walk.

        Regression: the earlier groups have already been walked (and, outside dry-run,
        really deleted) by the time a raised TypeError would abort the cycle.
        """
        client = MagicMock()
        client.list_groups.return_value = ["bogus", {"name": "later-grp"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.groups_processed == 1
        client.list_projects.assert_called_once_with("later-grp")

    def test_unhashable_group_name_skipped_not_fatal(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": ["not", "a", "string"]}, {"name": "good-grp"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"good-grp"}))
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.groups_processed == 1
        client.list_projects.assert_called_once_with("good-grp")

    def test_non_string_project_name_does_not_abort_mid_walk(self):
        """Regression: seen_projects.add() ran before the scope filter could skip the
        entry, so an unhashable name aborted the cycle after real deletions."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.side_effect = [
            [{"name": "proj-a"}],
            [{"name": ["not", "a", "string"]}, {"name": "proj-b"}],
        ]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.projects_processed == 2  # proj-a and proj-b, the malformed one skipped
        client.list_packages.assert_any_call("grp2", "proj-b")

    def test_blank_group_name_skipped(self):
        """An empty or whitespace-only name would build a URL with an empty path segment."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": ""}, {"name": "   "}, {"name": "grp"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 2
        assert stats.groups_processed == 1
        client.list_projects.assert_called_once_with("grp")

    def test_hashable_non_string_name_skipped(self):
        """The guard rejects any non-string name, not only unhashable ones — an int
        would otherwise be interpolated straight into the URL path."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": 7}, {"name": "grp"}]
        client.list_projects.return_value = [{"name": 7}, {"name": "proj"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 2
        assert stats.groups_processed == 1
        assert stats.projects_processed == 1
        client.list_projects.assert_called_once_with("grp")
        client.list_packages.assert_called_once_with("grp", "proj")

    def test_duplicate_project_is_walked_once(self):
        """Under DRY_RUN=false projects re-list from the API on each pass, so a duplicate
        costs wasted requests and an inflated projects_processed, not a second DELETE. In
        dry-run it double-counts `deleted` like every other level."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}, {"name": "proj"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 1
        client.list_packages.assert_called_once_with("grp", "proj")

    def test_duplicate_group_warns_even_when_out_of_scope(self, caplog):
        """The dedupe check sits above the scope filter on purpose — a misbehaving server's
        duplicates are worth surfacing even for entries we would not walk."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "out"}, {"name": "out"}, {"name": "in"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"in"}))
        with caplog.at_level("WARNING"):
            cleaner.run_cycle()

        assert any("Duplicate group entry 'out'" in r.message for r in caplog.records)
        client.list_projects.assert_called_once_with("in")

    def test_duplicate_project_warns_even_when_out_of_scope(self, caplog):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "out"}, {"name": "out"}, {"name": "in"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"in"}))
        with caplog.at_level("WARNING"):
            cleaner.run_cycle()

        assert any("Duplicate project entry grp/out" in r.message for r in caplog.records)
        client.list_packages.assert_called_once_with("grp", "in")

    def test_same_package_name_in_two_projects_is_not_deduped(self):
        """The package gate is per project — hoisting it to per-group or per-cycle would
        silently under-delete and blame the API for a duplicate it never returned."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj-a"}, {"name": "proj-b"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 2
        assert client.delete_package.call_args_list == [
            call("grp", "proj-a", "pkg"),
            call("grp", "proj-b", "pkg"),
        ]

    def test_duplicate_entries_warn_without_counting_an_error(self, caplog):
        """Duplicates are log-only by design: nothing failed to be evaluated, so `errors`
        stays clean. Pinned because it is the summary line alerting keys off."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}, {"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}, {"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}, {"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}, {"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False)
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        assert stats.errors == 0
        assert stats.deleted == 1
        assert sum("already seen" in r.message for r in caplog.records) == 4

    def test_duplicate_version_does_not_inflate_the_logged_count(self, caplog):
        """len(versions) would report `(2 versions)` for one real version, and that log
        line is what an operator reads back."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}, {"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=True)
        with caplog.at_level("INFO"):
            cleaner.run_cycle()

        assert any("WOULD DELETE grp/proj/pkg (1 versions)" in r.message for r in caplog.records)

    def test_duplicate_version_is_still_status_checked(self):
        """The version gate deliberately does not skip the /status/ call. A misbehaving
        server that repeats a version name with a divergent status must keep the fresh
        entry's power to veto the delete — one wasted request beats deleting on a
        half-read package. Costs one redundant call when the statuses agree."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}, {"version": "1.0"}]
        client.get_version_status.side_effect = [
            _status_response(_OLD_TIMESTAMP),
            _status_response(_FRESH_TIMESTAMP),
        ]

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert client.get_version_status.call_count == 2
        assert stats.deleted == 0
        assert stats.skipped == 1
        client.delete_package.assert_not_called()

    def test_same_version_in_two_packages_is_not_deduped(self, caplog):
        """The version gate is per package — hoisting it to per-project would report a
        sibling package's version name as a duplicate of this one. Since the gate no
        longer skips the status check, the warning is the whole observable effect, and a
        warning naming a version the server only sent once is a false accusation."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg-a"}, {"name": "pkg-b"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False)
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        assert stats.deleted == 2
        assert not [r for r in caplog.records if "already seen" in r.message]

    def test_duplicate_package_is_evaluated_once(self):
        """A package listing is iterated in memory with no re-list between deletes, so
        under DRY_RUN=false the repeat reaches a package that is already gone — dying one
        call short of DELETE, at list_versions. Uses a stateful fake because a MagicMock
        hands back the same listing forever: with the gate removed its fixture reports two
        successful deletes and no error, which is not what the portal would do."""
        portal = _StatefulPortal({"grp": {"proj": {"pkg": ["1.0"]}}}, duplicate_packages=True)

        cleaner = _make_cleaner(client=portal, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.packages_evaluated == 1
        assert stats.deleted == 1
        assert stats.errors == 0
        assert portal.delete_calls == [("grp", "proj", "pkg")]
        # The fake really removes it, so a second pass would have had something to hit.
        assert portal.tree["grp"]["proj"] == {}

    def test_same_project_name_in_two_groups_is_not_deduped(self):
        """The project gate is per group — the same name in another group is a
        different project and must still be walked."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.return_value = [{"name": "shared"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 2
        assert client.list_packages.call_count == 2

    def test_duplicate_group_is_walked_once(self):
        """In dry-run — the default — a repeat double-counts `deleted`. Under
        DRY_RUN=false groups re-list, so there it costs wasted requests and inflated
        groups_processed / packages_evaluated rather than a second DELETE."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "dup"}, {"name": "dup"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 1
        assert stats.packages_evaluated == 1
        assert stats.deleted == 1
        client.delete_package.assert_called_once_with("dup", "proj", "pkg")

    def test_non_dict_project_entry_skipped(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = ["bogus", {"name": "proj"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.projects_processed == 1

    def test_non_dict_package_entry_skipped(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = ["bogus", {"name": "pkg"}]
        client.list_versions.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.packages_evaluated == 1

    def test_non_dict_version_entry_skips_package(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = ["bogus"]

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.deleted == 0
        client.delete_package.assert_not_called()

    def test_bad_item_does_not_block_good_items(self):
        """A malformed group entry should not prevent processing subsequent groups."""
        client = MagicMock()
        client.list_groups.return_value = [{"id": "bad"}, {"name": "good-grp"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.groups_processed == 1


# ---------------------------------------------------------------------------
# Malformed listing *containers* (not entries)
# ---------------------------------------------------------------------------


class TestMalformedListingContainer:
    """`data.get("groups", [])` defaults only when the key is absent — a present-but-null
    value returns None, and iterating it raises. That is the same abort-mid-walk failure
    `_entry_name` exists to prevent, one level out: a package is already permanently
    deleted, later groups are never walked, no summary line prints, and the exit code
    still says success. Every listing container is therefore type-checked at its loop,
    where an error can be counted and the listing marked incomplete.
    """

    def test_null_group_listing_aborts_without_raising(self):
        client = MagicMock()
        client.list_groups.return_value = None

        stats = _make_cleaner(client=client).run_cycle()

        assert stats.errors == 1
        assert stats.groups_processed == 0

    def test_null_project_listing_does_not_abort_the_cycle(self):
        """The consequence that matters: groups after the bad one still get walked, so a
        single misbehaving group cannot make stale packages immortal across every cycle."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "g-a"}, {"name": "g-b"}, {"name": "g-c"}]
        client.list_projects.side_effect = lambda g: None if g == "g-b" else [{"name": "p"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.deleted == 2
        assert client.delete_package.call_args_list == [
            call("g-a", "p", "pkg"),
            call("g-c", "p", "pkg"),
        ]

    def test_null_project_listing_suppresses_the_project_warning(self, caplog):
        """Same standing as a listing that failed outright — we cannot conclude anything
        about a project filter from a container we could not read."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = None

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"proj-x"}))
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert not any("proj-x" in r.message for r in caplog.records)

    def test_null_package_listing_skips_the_project(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = None

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.packages_evaluated == 0
        client.delete_package.assert_not_called()

    def test_null_version_listing_skips_the_package(self):
        """Fail-safe: a package whose versions cannot be read is never deleted."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = None

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert stats.deleted == 0
        client.delete_package.assert_not_called()

    def test_non_dict_status_payload_skips_the_package(self):
        """`_get` returns whatever the body decoded to, so a bare `null` reaches
        _extract_timestamp. It must skip the package, not raise."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = None

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 0
        assert stats.skipped == 1
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# Empty tree scenarios
# ---------------------------------------------------------------------------


class TestEmptyTree:
    def test_empty_groups_list(self):
        client = MagicMock()
        client.list_groups.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 0
        assert stats.deleted == 0
        assert stats.errors == 0

    def test_empty_projects_list(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 1
        assert stats.projects_processed == 0
        assert stats.deleted == 0

    def test_empty_packages_list(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 1
        assert stats.packages_evaluated == 0
        assert stats.deleted == 0


# ---------------------------------------------------------------------------
# Non-UTC timezone comparison
# ---------------------------------------------------------------------------


class TestTimezoneComparison:
    def test_non_utc_offset_fresh_timestamp_not_deleted(self):
        """A fresh timestamp with a non-UTC offset should still be treated as fresh."""
        # This timestamp is 1 hour ago in UTC+12, which is 11 hours in the future UTC
        fresh_ts = (datetime.now(UTC) + timedelta(hours=11)).strftime("%Y-%m-%dT%H:%M:%S+12:00")

        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(fresh_ts)

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 0
        assert stats.skipped == 1
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-package deletion correctness: verify WHICH packages get deleted
# ---------------------------------------------------------------------------


class TestDeletionCorrectness:
    def test_only_stale_packages_deleted_in_multi_package_scenario(self):
        """In a mixed scenario, verify delete is called with exactly the right arguments."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [
            {"name": "stale-pkg"},
            {"name": "fresh-pkg"},
            {"name": "also-stale"},
        ]
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

        cleaner = _make_cleaner(client=client, dry_run=False)
        stats = cleaner.run_cycle()

        assert stats.deleted == 2
        assert stats.skipped == 1

        delete_calls = client.delete_package.call_args_list
        assert len(delete_calls) == 2
        assert delete_calls[0].args == ("grp", "proj", "stale-pkg")
        assert delete_calls[1].args == ("grp", "proj", "also-stale")


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------


class TestShutdownHandling:
    def test_shutdown_before_any_group(self):
        """Shutdown set before cycle starts processing groups."""
        shutdown = threading.Event()
        shutdown.set()

        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]

        cleaner = _make_cleaner(client=client, shutdown=shutdown)
        stats = cleaner.run_cycle()

        assert stats.interrupted is True
        assert stats.groups_processed == 0
        assert stats.deleted == 0
        client.list_projects.assert_not_called()

    def test_shutdown_after_first_group_skips_second(self):
        """Shutdown set during first group prevents second group from processing."""
        shutdown = threading.Event()

        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.return_value = []

        def set_shutdown_on_first_group(group):
            shutdown.set()
            return []

        client.list_projects.side_effect = set_shutdown_on_first_group

        cleaner = _make_cleaner(client=client, shutdown=shutdown)
        stats = cleaner.run_cycle()

        assert stats.interrupted is True
        assert stats.groups_processed == 1
        client.list_projects.assert_called_once_with("grp1")

    def test_shutdown_prevents_deletion_even_when_all_stale(self):
        """Most critical: shutdown must block deletion even if all versions are stale."""
        shutdown = threading.Event()

        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        def set_shutdown_after_status(*args, **kwargs):
            result = _status_response(_OLD_TIMESTAMP)
            shutdown.set()
            return result

        client.get_version_status.side_effect = set_shutdown_after_status

        cleaner = _make_cleaner(client=client, dry_run=False, shutdown=shutdown)
        stats = cleaner.run_cycle()

        assert stats.interrupted is True
        assert stats.deleted == 0
        client.delete_package.assert_not_called()

    def test_shutdown_mid_version_evaluation_abandons_package(self):
        """Shutdown during version loop stops evaluating further versions."""
        shutdown = threading.Event()

        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [
            {"version": "1.0"},
            {"version": "2.0"},
            {"version": "3.0"},
        ]

        def set_shutdown_on_first_version(*args, **kwargs):
            shutdown.set()
            return _status_response(_OLD_TIMESTAMP)

        client.get_version_status.side_effect = set_shutdown_on_first_version

        cleaner = _make_cleaner(client=client, dry_run=False, shutdown=shutdown)
        stats = cleaner.run_cycle()

        assert stats.interrupted is True
        assert stats.deleted == 0
        # Only checked one version before shutdown was detected
        assert client.get_version_status.call_count == 1
        client.delete_package.assert_not_called()

    def test_default_shutdown_event_never_interrupts(self):
        """Backward compat: default Event is never set, so cycle completes normally."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=True)
        stats = cleaner.run_cycle()

        assert stats.interrupted is False
        assert stats.deleted == 1

    def test_cycle_stats_interrupted_defaults_false(self):
        assert CycleStats().interrupted is False

    def test_partial_stats_reflect_work_before_interruption(self):
        """Stats should reflect only the work done before shutdown."""
        shutdown = threading.Event()

        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [
            {"name": "pkg-1"},
            {"name": "pkg-2"},
            {"name": "pkg-3"},
        ]

        call_count = 0

        def list_versions_with_shutdown(group, project, package):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                shutdown.set()
            return [{"version": "1.0"}]

        client.list_versions.side_effect = list_versions_with_shutdown
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=True, shutdown=shutdown)
        stats = cleaner.run_cycle()

        assert stats.interrupted is True
        # First package fully processed (evaluated + deleted in dry-run)
        # Second package entered _evaluate_package, but shutdown detected at version loop
        assert stats.packages_evaluated == 2
        assert stats.deleted == 1


# ---------------------------------------------------------------------------
# Group scoping
# ---------------------------------------------------------------------------


class TestGroupScoping:
    def test_only_matching_group_is_walked(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"grp1"}))
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 1
        client.list_projects.assert_called_once_with("grp1")

    def test_multiple_groups_in_scope(self):
        client = MagicMock()
        client.list_groups.return_value = [
            {"name": "grp1"},
            {"name": "grp2"},
            {"name": "grp3"},
        ]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"grp1", "grp3"}))
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 2
        walked = {call.args[0] for call in client.list_projects.call_args_list}
        assert walked == {"grp1", "grp3"}

    def test_no_group_filter_walks_all(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client)
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 2
        assert client.list_projects.call_count == 2

    def test_nonmatching_group_filter_deletes_nothing(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False, target_groups=frozenset({"other"}))
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 0
        assert stats.deleted == 0
        client.list_projects.assert_not_called()
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# Project scoping
# ---------------------------------------------------------------------------


class TestProjectScoping:
    def test_only_matching_project_is_walked(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj-a"}, {"name": "proj-b"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"proj-a"}))
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 1
        client.list_packages.assert_called_once_with("grp", "proj-a")

    def test_project_filter_spans_all_groups(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.side_effect = [
            [{"name": "proj-x"}, {"name": "proj-y"}],
            [{"name": "proj-x"}, {"name": "proj-z"}],
        ]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"proj-x"}))
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 2
        walked = {call.args for call in client.list_packages.call_args_list}
        assert walked == {("grp1", "proj-x"), ("grp2", "proj-x")}

    def test_group_and_project_filter_together(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        client.list_projects.return_value = [{"name": "proj-a"}, {"name": "proj-b"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(
            client=client,
            target_groups=frozenset({"grp1"}),
            target_projects=frozenset({"proj-b"}),
        )
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 1
        assert stats.projects_processed == 1
        client.list_packages.assert_called_once_with("grp1", "proj-b")

    def test_nonmatching_project_filter_deletes_nothing(self):
        """The mirror of the group-filter test: a deletable package behind an
        out-of-scope project must never be reached, with dry_run off."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "proj"}]
        client.list_packages.return_value = [{"name": "pkg"}]
        client.list_versions.return_value = [{"version": "1.0"}]
        client.get_version_status.return_value = _status_response(_OLD_TIMESTAMP)

        cleaner = _make_cleaner(client=client, dry_run=False, target_projects=frozenset({"other"}))
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 0
        assert stats.deleted == 0
        client.list_packages.assert_not_called()
        client.delete_package.assert_not_called()


# ---------------------------------------------------------------------------
# Scope matching semantics — exact, case-sensitive, never substring
# ---------------------------------------------------------------------------


class TestScopeMatchSemantics:
    """The README makes exact matching the safety argument for scoping, so pin it.

    A case test does not catch a substring mutation and vice versa, so both
    directions are asserted for each filter.
    """

    def test_group_match_is_case_sensitive(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "Platform"}]

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"platform"}))
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 0
        client.list_projects.assert_not_called()

    def test_group_match_is_exact_not_substring(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "prod-sandbox"}]

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"prod"}))
        stats = cleaner.run_cycle()

        assert stats.groups_processed == 0
        client.list_projects.assert_not_called()

    def test_project_match_is_case_sensitive(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "Api"}]

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"api"}))
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 0
        client.list_packages.assert_not_called()

    def test_project_match_is_exact_not_substring(self):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp"}]
        client.list_projects.return_value = [{"name": "api-legacy"}, {"name": "internal-api"}]

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"api"}))
        stats = cleaner.run_cycle()

        assert stats.projects_processed == 0
        client.list_packages.assert_not_called()


# ---------------------------------------------------------------------------
# Unmatched scope warnings
# ---------------------------------------------------------------------------


class TestScopeWarnings:
    def test_unmatched_group_warns(self, caplog):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.return_value = []

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"nope"}))
        with caplog.at_level("WARNING"):
            cleaner.run_cycle()

        assert any("nope" in r.message for r in caplog.records)

    def test_unmatched_project_warns(self, caplog):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.return_value = [{"name": "proj-a"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"nope"}))
        with caplog.at_level("WARNING"):
            cleaner.run_cycle()

        assert any("nope" in r.message for r in caplog.records)

    def test_matched_filters_emit_no_warning(self, caplog):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.return_value = [{"name": "proj-a"}]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(
            client=client,
            target_groups=frozenset({"grp1"}),
            target_projects=frozenset({"proj-a"}),
        )
        with caplog.at_level("WARNING"):
            cleaner.run_cycle()

        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    def test_project_only_in_out_of_scope_group_warns(self, caplog):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]
        # proj-x only exists in grp2, which is excluded by the group filter.
        client.list_projects.side_effect = [[{"name": "proj-a"}], [{"name": "proj-x"}]]
        client.list_packages.return_value = []

        cleaner = _make_cleaner(
            client=client,
            target_groups=frozenset({"grp1"}),
            target_projects=frozenset({"proj-x"}),
        )
        with caplog.at_level("WARNING"):
            cleaner.run_cycle()

        assert any("proj-x" in r.message for r in caplog.records)

    def test_project_warning_suppressed_when_group_filter_unmatched(self, caplog):
        """A group typo must not manufacture phantom project typos.

        proj-a exists in grp1, but the unmatched group filter means no project
        listing ever happened — reporting proj-a as unmatched would be false.
        """
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.return_value = [{"name": "proj-a"}]

        cleaner = _make_cleaner(
            client=client,
            target_groups=frozenset({"grp-typo"}),
            target_projects=frozenset({"proj-a"}),
        )
        with caplog.at_level("WARNING"):
            cleaner.run_cycle()

        assert any("grp-typo" in r.message for r in caplog.records)
        assert not any("proj-a" in r.message for r in caplog.records)

    def test_project_warning_suppressed_when_group_listing_errors(self, caplog):
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.side_effect = APIError("GET", "url", 500, "err")

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"proj-x"}))
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        assert stats.errors == 1
        # The group's project listing failed, so we cannot conclude proj-x is unmatched.
        assert not any("proj-x" in r.message for r in caplog.records)

    def test_project_warning_suppressed_when_a_project_entry_is_malformed(self, caplog):
        """A listing that failed and a listing with an unreadable entry in it leave us in
        the same position: the filter's project may have been one of the names we could
        not read. Only the failed listing used to suppress the warning."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.return_value = [{"name": 7}]

        cleaner = _make_cleaner(client=client, target_projects=frozenset({"proj-x"}))
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert any("Malformed project entry" in r.message for r in caplog.records)
        assert not any("proj-x" in r.message for r in caplog.records)

    def test_both_warnings_suppressed_when_a_group_entry_is_malformed(self, caplog):
        """Same reasoning one level up, and it reaches both levels: an unreadable group
        entry may be the group a filter names, and its projects were never listed at all."""
        client = MagicMock()
        client.list_groups.return_value = [{"name": 7}]

        cleaner = _make_cleaner(
            client=client,
            target_groups=frozenset({"grp-x"}),
            target_projects=frozenset({"proj-x"}),
        )
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        assert stats.errors == 1
        assert any("Malformed group entry" in r.message for r in caplog.records)
        assert not any("grp-x" in r.message or "proj-x" in r.message for r in caplog.records)

    def test_a_malformed_project_entry_does_not_silence_a_group_typo(self, caplog):
        """The project-level suppression must not be widened to cover group warnings —
        that would silence a real typo, the exact failure the suppression was written to
        avoid, one channel over.

        The group filter has to both match and contain a typo. With only the typo, the
        one real group fails the scope filter, `_process_group` is never called, and the
        malformed entry is never read — which is how the previous version of this test
        passed without exercising anything.
        """
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}]
        client.list_projects.return_value = [{"name": 7}]

        cleaner = _make_cleaner(client=client, target_groups=frozenset({"grp1", "grp-typo"}))
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        # Proves the malformed entry was actually reached this time.
        assert stats.errors == 1
        assert any("Malformed project entry" in r.message for r in caplog.records)
        assert any("grp-typo" in r.message for r in caplog.records)

    def test_no_warnings_when_interrupted(self, caplog):
        shutdown = threading.Event()
        client = MagicMock()
        client.list_groups.return_value = [{"name": "grp1"}, {"name": "grp2"}]

        def stop_during_walk(group):
            shutdown.set()
            return []

        client.list_projects.side_effect = stop_during_walk

        # These filters are unmatched, but the interrupt must suppress the
        # end-of-cycle warning entirely.
        cleaner = _make_cleaner(
            client=client,
            shutdown=shutdown,
            target_projects=frozenset({"unmatched-proj"}),
        )
        with caplog.at_level("WARNING"):
            stats = cleaner.run_cycle()

        assert stats.interrupted is True
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

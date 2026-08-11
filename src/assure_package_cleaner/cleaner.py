"""Core cleanup logic — walks the portal tree and deletes stale packages."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from assure_package_cleaner.client import APIError, SpectraClient

logger = logging.getLogger(__name__)


@dataclass
class CycleStats:
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    groups_processed: int = 0
    projects_processed: int = 0
    packages_evaluated: int = 0
    interrupted: bool = False


@dataclass
class Cleaner:
    client: SpectraClient
    stale_threshold_days: int
    dry_run: bool = True
    shutdown: threading.Event = field(default_factory=threading.Event)
    target_groups: frozenset[str] = frozenset()
    target_projects: frozenset[str] = frozenset()

    def _check_shutdown(self) -> bool:
        return self.shutdown.is_set()

    def run_cycle(self) -> CycleStats:
        cutoff = datetime.now(UTC) - timedelta(days=self.stale_threshold_days)
        stats = CycleStats()

        logger.info(
            "Starting cleanup cycle — cutoff=%s (packages older than %d days)",
            cutoff.isoformat(),
            self.stale_threshold_days,
        )

        try:
            groups = self.client.list_groups()
        except APIError:
            logger.exception("Failed to list groups — aborting cycle")
            stats.errors += 1
            return stats

        # `groups` is iterated twice — here and in the loop below — so the client must keep
        # returning a concrete list rather than a generator.
        group_names = {name for g in groups if (name := _entry_name(g)) is not None}
        seen_projects: set[str] = set()
        walked_groups: set[str] = set()
        projects_fully_listed = True

        for group in groups:
            if self._check_shutdown():
                stats.interrupted = True
                break
            group_name = _entry_name(group)
            if group_name is None:
                logger.warning("Malformed group entry: %r — skipping", group)
                stats.errors += 1
                continue
            if group_name in walked_groups:
                # Walking a repeated group would evaluate its packages twice, inflating
                # `deleted` and turning the second DELETE into a 404 counted as an error.
                logger.warning("Duplicate group entry %r — already walked, skipping", group_name)
                continue
            walked_groups.add(group_name)
            if self.target_groups and group_name not in self.target_groups:
                logger.debug("Group %s not in scope — skipping", group_name)
                continue
            stats.groups_processed += 1
            if not self._process_group(group_name, cutoff, stats, seen_projects):
                projects_fully_listed = False

        if not stats.interrupted:
            self._warn_unmatched(group_names, seen_projects, projects_fully_listed)

        status = "Cycle interrupted" if stats.interrupted else "Cycle complete"
        logger.info(
            "%s — deleted=%d skipped=%d errors=%d (groups=%d projects=%d packages=%d)",
            status,
            stats.deleted,
            stats.skipped,
            stats.errors,
            stats.groups_processed,
            stats.projects_processed,
            stats.packages_evaluated,
        )
        return stats

    def _process_group(
        self, group: str, cutoff: datetime, stats: CycleStats, seen_projects: set[str]
    ) -> bool:
        """Walk a group's projects. Returns False if the project listing failed."""
        try:
            projects = self.client.list_projects(group)
        except APIError:
            logger.exception("Failed to list projects in group %s — skipping group", group)
            stats.errors += 1
            return False

        for project in projects:
            if self._check_shutdown():
                stats.interrupted = True
                return True
            project_name = _entry_name(project)
            if project_name is None:
                logger.warning("Malformed project entry in group %s: %r — skipping", group, project)
                stats.errors += 1
                continue
            seen_projects.add(project_name)
            if self.target_projects and project_name not in self.target_projects:
                logger.debug("Project %s/%s not in scope — skipping", group, project_name)
                continue
            stats.projects_processed += 1
            self._process_project(group, project_name, cutoff, stats)
        return True

    def _warn_unmatched(
        self, group_names: set[str], seen_projects: set[str], projects_fully_listed: bool
    ) -> None:
        unmatched_groups = self.target_groups - group_names
        for group in sorted(unmatched_groups):
            logger.warning("Group filter %r matched no group in the org", group)
        # A group filter that matched nothing means no project listing ever happened for it,
        # so every project filter would look unmatched — suppress the phantom warnings.
        if projects_fully_listed and not unmatched_groups:
            for project in sorted(self.target_projects - seen_projects):
                logger.warning("Project filter %r matched no project in scope", project)

    def _process_project(
        self, group: str, project: str, cutoff: datetime, stats: CycleStats
    ) -> None:
        try:
            packages = self.client.list_packages(group, project)
        except APIError:
            logger.exception("Failed to list packages in %s/%s — skipping project", group, project)
            stats.errors += 1
            return

        for package in packages:
            if self._check_shutdown():
                stats.interrupted = True
                return
            package_name = _entry_name(package)
            if package_name is None:
                logger.warning(
                    "Malformed package entry in %s/%s: %r — skipping", group, project, package
                )
                stats.errors += 1
                continue
            stats.packages_evaluated += 1
            self._evaluate_package(group, project, package_name, cutoff, stats)

    def _evaluate_package(
        self,
        group: str,
        project: str,
        package: str,
        cutoff: datetime,
        stats: CycleStats,
    ) -> None:
        pkg_path = f"{group}/{project}/{package}"

        try:
            versions = self.client.list_versions(group, project, package)
        except APIError:
            logger.exception("Failed to list versions for %s — skipping package", pkg_path)
            stats.errors += 1
            return

        if not versions:
            logger.debug("No versions found for %s — skipping", pkg_path)
            stats.skipped += 1
            return

        all_stale = True
        for version_info in versions:
            if self._check_shutdown():
                stats.interrupted = True
                return
            version = _entry_name(version_info, key="version")
            if version is None:
                logger.warning(
                    "Malformed version entry in %s: %r — skipping package (fail-safe)",
                    pkg_path,
                    version_info,
                )
                stats.errors += 1
                return
            try:
                status = self.client.get_version_status(group, project, package, version)
            except APIError:
                logger.exception(
                    "Failed to get status for %s@%s — skipping package (fail-safe)",
                    pkg_path,
                    version,
                )
                stats.errors += 1
                return

            timestamp_str = _extract_timestamp(status)
            if timestamp_str is None:
                logger.warning(
                    "No analysis timestamp for %s@%s — skipping package (fail-safe)",
                    pkg_path,
                    version,
                )
                stats.skipped += 1
                return

            try:
                analysis_time = _parse_timestamp(timestamp_str)
            except ValueError:
                logger.warning(
                    "Unparseable timestamp %r for %s@%s — skipping package (fail-safe)",
                    timestamp_str,
                    pkg_path,
                    version,
                )
                stats.errors += 1
                return

            if analysis_time >= cutoff:
                logger.debug(
                    "Version %s@%s analyzed at %s is newer than cutoff — skipping package",
                    pkg_path,
                    version,
                    analysis_time.isoformat(),
                )
                all_stale = False
                break

        if all_stale:
            if self._check_shutdown():
                stats.interrupted = True
                return
            self._delete_package(group, project, package, len(versions), stats)
        else:
            stats.skipped += 1

    def _delete_package(
        self,
        group: str,
        project: str,
        package: str,
        version_count: int,
        stats: CycleStats,
    ) -> None:
        pkg_path = f"{group}/{project}/{package}"

        if self.dry_run:
            logger.info("WOULD DELETE %s (%d versions)", pkg_path, version_count)
            stats.deleted += 1
            return

        try:
            self.client.delete_package(group, project, package)
        except APIError:
            logger.exception("Failed to delete %s", pkg_path)
            stats.errors += 1
            return

        logger.info("DELETED %s (%d versions)", pkg_path, version_count)
        stats.deleted += 1


def _entry_name(entry: object, key: str = "name") -> str | None:
    """Pull a string name out of a listing entry, or None if the entry is malformed.

    Every walk loop routes through this. A non-dict entry, a missing key, or a non-string
    value must all skip the entry rather than raise — an exception mid-walk aborts the
    cycle after earlier entries have already had their packages deleted. A non-string name
    also has to be caught before it reaches a set operation or a scope check, both of which
    raise on an unhashable value.
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    # Returned unstripped: matching is exact against what the API reports, and the name
    # goes into a URL path as-is. Only a wholly blank name is rejected, since that would
    # build a path with an empty segment.
    return value


def _extract_timestamp(status: dict) -> str | None:
    analysis = status.get("analysis")
    if not isinstance(analysis, dict):
        return None
    ts = analysis.get("timestamp")
    return ts if isinstance(ts, str) else None


def _parse_timestamp(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt

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

        for group in groups:
            if self._check_shutdown():
                stats.interrupted = True
                break
            try:
                group_name = group["name"]
            except KeyError:
                logger.warning("Group entry missing 'name' key: %r — skipping", group)
                stats.errors += 1
                continue
            stats.groups_processed += 1
            self._process_group(group_name, cutoff, stats)

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

    def _process_group(self, group: str, cutoff: datetime, stats: CycleStats) -> None:
        try:
            projects = self.client.list_projects(group)
        except APIError:
            logger.exception("Failed to list projects in group %s — skipping group", group)
            stats.errors += 1
            return

        for project in projects:
            if self._check_shutdown():
                stats.interrupted = True
                return
            try:
                project_name = project["name"]
            except KeyError:
                logger.warning("Project entry missing 'name' key in group %s — skipping", group)
                stats.errors += 1
                continue
            stats.projects_processed += 1
            self._process_project(group, project_name, cutoff, stats)

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
            try:
                package_name = package["name"]
            except KeyError:
                logger.warning(
                    "Package entry missing 'name' key in %s/%s — skipping", group, project
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
            try:
                version = version_info["version"]
            except KeyError:
                logger.warning(
                    "Version entry missing 'version' key in %s — skipping package (fail-safe)",
                    pkg_path,
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

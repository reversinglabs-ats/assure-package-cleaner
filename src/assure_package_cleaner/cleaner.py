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

        # The summary line is the only aggregate an operator gets, and every abort path is
        # exactly when they need it most, so `finally` makes it unconditional. The status
        # word has to carry the difference: "Cycle complete" on a cycle that walked
        # nothing is a false all-clear, and worse than the no-line-at-all it replaced,
        # because a scheduler watching for a missing line would no longer see one.
        raised = True
        walked = False
        try:
            walked = self._walk(cutoff, stats)
            raised = False
        finally:
            if raised or not walked:
                # Either an exception escaped, or the walk never got past listing groups.
                status = "Cycle ABORTED"
            elif stats.interrupted:
                status = "Cycle interrupted"
            else:
                status = "Cycle complete"
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

    def _walk(self, cutoff: datetime, stats: CycleStats) -> bool:
        """Walk the tree. Returns False if the group listing never yielded anything to walk.

        The return value feeds the summary line's status word: an aborted cycle must not
        be reported as a complete one.
        """
        try:
            groups = self.client.list_groups()
        except APIError:
            logger.exception("Failed to list groups — aborting cycle")
            stats.errors += 1
            return False

        if not isinstance(groups, list):
            logger.error("Group listing is not a list: %s — aborting cycle", _brief(groups))
            stats.errors += 1
            return False

        seen_projects: set[str] = set()
        walked_groups: set[str] = set()
        groups_fully_listed = True
        projects_fully_listed = True

        for group in groups:
            if self._check_shutdown():
                stats.interrupted = True
                break
            group_name = _entry_name(group)
            if group_name is None:
                logger.warning("Malformed group entry: %s — skipping", _brief(group))
                stats.errors += 1
                groups_fully_listed = False
                continue
            # Checked above the scope filter on purpose: a misbehaving server's duplicates
            # are worth surfacing even for groups we would not walk.
            if group_name in walked_groups:
                logger.warning(
                    "Duplicate group entry %s — already seen, skipping", _brief(group_name)
                )
                continue
            walked_groups.add(group_name)
            if self.target_groups and group_name not in self.target_groups:
                logger.debug("Group %s not in scope — skipping", group_name)
                continue
            stats.groups_processed += 1
            if not self._process_group(group_name, cutoff, stats, seen_projects):
                projects_fully_listed = False

        if not stats.interrupted:
            self._warn_unmatched(
                walked_groups, seen_projects, groups_fully_listed, projects_fully_listed
            )
        return True

    def _process_group(
        self, group: str, cutoff: datetime, stats: CycleStats, seen_projects: set[str]
    ) -> bool:
        """Walk a group's projects. Returns False if the projects were not fully enumerated."""
        try:
            projects = self.client.list_projects(group)
        except APIError:
            logger.exception("Failed to list projects in group %s — skipping group", group)
            stats.errors += 1
            return False

        if not isinstance(projects, list):
            logger.error(
                "Project listing in group %s is not a list: %s — skipping group",
                group,
                _brief(projects),
            )
            stats.errors += 1
            return False

        # Per group, not global: the same project name legitimately appears in many groups.
        walked_projects: set[str] = set()
        fully_listed = True

        for project in projects:
            if self._check_shutdown():
                stats.interrupted = True
                return True
            project_name = _entry_name(project)
            if project_name is None:
                logger.warning(
                    "Malformed project entry in group %s: %s — skipping", group, _brief(project)
                )
                stats.errors += 1
                fully_listed = False
                continue
            if project_name in walked_projects:
                logger.warning(
                    "Duplicate project entry %s/%s — already seen, skipping", group, project_name
                )
                continue
            walked_projects.add(project_name)
            seen_projects.add(project_name)
            if self.target_projects and project_name not in self.target_projects:
                logger.debug("Project %s/%s not in scope — skipping", group, project_name)
                continue
            stats.projects_processed += 1
            self._process_project(group, project_name, cutoff, stats)
        return fully_listed

    def _warn_unmatched(
        self,
        group_names: set[str],
        seen_projects: set[str],
        groups_fully_listed: bool,
        projects_fully_listed: bool,
    ) -> None:
        # A group entry we could not read might have been the one a filter names, so it
        # makes both levels look unmatched — that group's projects were never listed
        # either. Same epistemic situation as a listing that failed outright.
        if not groups_fully_listed:
            return
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

        if not isinstance(packages, list):
            logger.error(
                "Package listing in %s/%s is not a list: %s — skipping project",
                group,
                project,
                _brief(packages),
            )
            stats.errors += 1
            return

        # Per project, not per group or per cycle: the same package name in a different
        # project is a different package and must still be evaluated.
        #
        # In dry-run — the default — a duplicate group, project or package double-counts
        # `deleted`, and that report is what an operator reads to decide whether to set
        # DRY_RUN=false. A duplicate version cannot: `deleted` counts packages.
        # Under DRY_RUN=false these three levels diverge: groups and projects re-list from
        # the API between passes, so the cost there is wasted requests and inflated
        # counters, while this listing is iterated in memory with no re-list, so the repeat
        # reaches a package that is already gone and 404s at `list_versions` into `errors`
        # — one call short of DELETE, which is never attempted twice.
        walked_packages: set[str] = set()

        for package in packages:
            if self._check_shutdown():
                stats.interrupted = True
                return
            package_name = _entry_name(package)
            if package_name is None:
                logger.warning(
                    "Malformed package entry in %s/%s: %s — skipping",
                    group,
                    project,
                    _brief(package),
                )
                stats.errors += 1
                continue
            if package_name in walked_packages:
                logger.warning(
                    "Duplicate package entry %s/%s/%s — already seen, skipping",
                    group,
                    project,
                    package_name,
                )
                continue
            walked_packages.add(package_name)
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

        if not isinstance(versions, list):
            logger.error(
                "Version listing for %s is not a list: %s — skipping package (fail-safe)",
                pkg_path,
                _brief(versions),
            )
            stats.errors += 1
            return

        if not versions:
            logger.debug("No versions found for %s — skipping", pkg_path)
            stats.skipped += 1
            return

        all_stale = True
        walked_versions: set[str] = set()
        for version_info in versions:
            if self._check_shutdown():
                stats.interrupted = True
                return
            version = _entry_name(version_info, key="version")
            if version is None:
                logger.warning(
                    "Malformed version entry in %s: %s — skipping package (fail-safe)",
                    pkg_path,
                    _brief(version_info),
                )
                stats.errors += 1
                return
            if version in walked_versions:
                # Warned and counted once for the DELETED log line, but deliberately NOT
                # skipped: if a misbehaving server reports the same version twice with
                # divergent statuses, the repeat must keep its power to veto the delete.
                # One redundant /status/ call is cheaper than deleting on a half-read
                # package — every other doubt in this walk skips, and so does this one.
                logger.warning("Duplicate version entry %s@%s — already seen", pkg_path, version)
            walked_versions.add(version)
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
                    "Unparseable timestamp %s for %s@%s — skipping package (fail-safe)",
                    _brief(timestamp_str),
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
            self._delete_package(group, project, package, len(walked_versions), stats)
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


def _brief(value: object, limit: int = 200) -> str:
    """repr() a value for a log line, truncated.

    Wraps every site that logs server-controlled data of unbounded size: the whole
    listing payload in the four container guards, each malformed entry, and the raw
    timestamp string. One hostile response should not be able to bury the rest of the
    cycle in a single log record.

    Note what this does *not* bound: a listing that is a genuine list of 50,000
    malformed entries passes the container guard and goes down the per-entry path, one
    truncated record each. That is 50,000 records — bounded per record, not in total.
    Tracked as #21.
    """
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}… ({len(text)} chars)"


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


def _extract_timestamp(status: object) -> str | None:
    # The whole payload is guarded, not just the values inside it: `_get` returns whatever
    # the endpoint decoded to, and a JSON body of `null`, `[]` or `"x"` would otherwise
    # raise AttributeError here and abort the cycle mid-walk.
    if not isinstance(status, dict):
        return None
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

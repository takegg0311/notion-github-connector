"""Sync GitHub snapshot into Notion databases."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Settings
from .github import (
    GitHubClientImpl,
    GitHubSnapshot,
    IssueRecord,
    PullRequestRecord,
    RepositoryRecord,
    issue_status_label,
    pull_request_status_label,
    resolve_issue_github_ids,
)
from .notion import (
    ISSUE_SCHEMA,
    PULL_REQUEST_SCHEMA,
    REPOSITORY_SCHEMA,
    DatabaseContext,
    NotionClientImpl,
    read_rich_text,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class WriteStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0


@dataclass
class SyncState:
    repo_page_by_github_id: dict[str, str] = field(default_factory=dict)
    issue_page_by_github_id: dict[str, str] = field(default_factory=dict)
    failures: bool = False


def normalize_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).replace(second=0, microsecond=0)


def datetime_minute_key(value: str | None) -> str | None:
    parsed = normalize_datetime(value)
    return parsed.isoformat() if parsed else None


def property_github_id(page: dict[str, Any]) -> str:
    prop = page.get("properties", {}).get("GitHub ID", {})
    if prop.get("type") != "rich_text":
        return ""
    return read_rich_text(prop.get("rich_text", [])).strip()


def relation_page_ids(page: dict[str, Any], property_name: str) -> tuple[frozenset[str], bool]:
    prop = page.get("properties", {}).get(property_name, {})
    if prop.get("type") != "relation":
        return frozenset(), False
    relation = prop.get("relation", [])
    ids = frozenset(item["id"] for item in relation if item.get("id"))
    return ids, bool(prop.get("has_more"))


def read_title(page: dict[str, Any]) -> str:
    prop = page.get("properties", {}).get("Name", {})
    return read_rich_text(prop.get("title", []))


def read_number(page: dict[str, Any]) -> int | None:
    prop = page.get("properties", {}).get("Number", {})
    if prop.get("type") != "number":
        return None
    return prop.get("number")


def read_status(page: dict[str, Any]) -> str | None:
    prop = page.get("properties", {}).get("Status", {})
    if prop.get("type") != "status":
        return None
    status = prop.get("status")
    return status.get("name") if status else None


def read_url(page: dict[str, Any]) -> str | None:
    prop = page.get("properties", {}).get("URL", {})
    if prop.get("type") != "url":
        return None
    return prop.get("url")


def read_date_start(page: dict[str, Any], property_name: str) -> str | None:
    prop = page.get("properties", {}).get(property_name, {})
    if prop.get("type") != "date":
        return None
    date = prop.get("date")
    return date.get("start") if date else None


def build_repository_properties(record: RepositoryRecord) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": record.name}}]},
        "URL": {"url": record.url},
        "Created": {"date": {"start": record.created_at}},
        "GitHub ID": {"rich_text": [{"text": {"content": record.github_id}}]},
    }
    if record.pushed_at:
        props["Updated"] = {"date": {"start": record.pushed_at}}
    else:
        props["Updated"] = {"date": None}
    return props


def build_issue_properties(
    record: IssueRecord, repository_page_id: str, status: str
) -> dict[str, Any]:
    return {
        "Name": {"title": [{"text": {"content": record.title}}]},
        "Number": {"number": record.number},
        "Status": {"status": {"name": status}},
        "URL": {"url": record.url},
        "Created": {"date": {"start": record.created_at}},
        "Updated": {"date": {"start": record.updated_at}},
        "Repository": {"relation": [{"id": repository_page_id}]},
        "GitHub ID": {"rich_text": [{"text": {"content": record.github_id}}]},
    }


def build_pull_request_properties(
    record: PullRequestRecord,
    repository_page_id: str,
    issue_page_ids: list[str],
    status: str,
) -> dict[str, Any]:
    return {
        "Name": {"title": [{"text": {"content": record.title}}]},
        "Number": {"number": record.number},
        "Status": {"status": {"name": status}},
        "URL": {"url": record.url},
        "Created": {"date": {"start": record.created_at}},
        "Updated": {"date": {"start": record.updated_at}},
        "Repository": {"relation": [{"id": repository_page_id}]},
        "Issues": {"relation": [{"id": page_id} for page_id in sorted(issue_page_ids)]},
        "GitHub ID": {"rich_text": [{"text": {"content": record.github_id}}]},
    }


def repository_needs_update(page: dict[str, Any], desired: dict[str, Any]) -> bool:
    if read_title(page) != desired["Name"]["title"][0]["text"]["content"]:
        return True
    if read_url(page) != desired["URL"]["url"]:
        return True
    if datetime_minute_key(read_date_start(page, "Created")) != datetime_minute_key(
        desired["Created"]["date"]["start"]
    ):
        return True
    desired_updated = desired["Updated"]["date"]
    desired_updated_start = desired_updated["start"] if desired_updated else None
    if datetime_minute_key(read_date_start(page, "Updated")) != datetime_minute_key(
        desired_updated_start
    ):
        return True
    if property_github_id(page) != desired["GitHub ID"]["rich_text"][0]["text"]["content"]:
        return True
    return False


def issue_or_pr_needs_update(
    page: dict[str, Any],
    desired: dict[str, Any],
    relation_property: str | None = None,
) -> bool:
    if read_title(page) != desired["Name"]["title"][0]["text"]["content"]:
        return True
    if read_number(page) != desired["Number"]["number"]:
        return True
    if read_status(page) != desired["Status"]["status"]["name"]:
        return True
    if read_url(page) != desired["URL"]["url"]:
        return True
    for date_prop in ("Created", "Updated"):
        if datetime_minute_key(read_date_start(page, date_prop)) != datetime_minute_key(
            desired[date_prop]["date"]["start"]
        ):
            return True
    if property_github_id(page) != desired["GitHub ID"]["rich_text"][0]["text"]["content"]:
        return True
    repo_ids, repo_has_more = relation_page_ids(page, "Repository")
    desired_repo_ids = frozenset(item["id"] for item in desired["Repository"]["relation"])
    if repo_has_more or repo_ids != desired_repo_ids:
        return True
    if relation_property:
        issue_ids, issue_has_more = relation_page_ids(page, relation_property)
        desired_issue_ids = frozenset(item["id"] for item in desired[relation_property]["relation"])
        if issue_has_more or issue_ids != desired_issue_ids:
            return True
    return False


def index_pages_by_github_id(
    pages: list[dict[str, Any]], db_name: str
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        github_id = property_github_id(page)
        if not github_id:
            continue
        grouped.setdefault(github_id, []).append(page)
    result: dict[str, dict[str, Any]] = {}
    for github_id, duplicates in grouped.items():
        if len(duplicates) > 1:
            LOGGER.warning(
                "%s: duplicate GitHub ID %s on %s pages; using oldest created_time",
                db_name,
                github_id,
                len(duplicates),
            )
        chosen = min(duplicates, key=lambda item: item.get("created_time", ""))
        result[github_id] = chosen
    return result


class SyncServiceImpl:
    def __init__(
        self,
        settings: Settings,
        notion: NotionClientImpl,
        github: GitHubClientImpl,
    ) -> None:
        self.settings = settings
        self.notion = notion
        self.github = github

    def run(self) -> bool:
        repo_context = self.notion.validate_database(
            "RepositoryDB",
            self.settings.repository_database_id,
            REPOSITORY_SCHEMA,
            {},
        )
        issue_context = self.notion.validate_database(
            "IssueDB",
            self.settings.issue_database_id,
            ISSUE_SCHEMA,
            {"Repository": repo_context.data_source_id},
        )
        pr_context = self.notion.validate_database(
            "PullRequestDB",
            self.settings.pull_request_database_id,
            PULL_REQUEST_SCHEMA,
            {
                "Repository": repo_context.data_source_id,
                "Issues": issue_context.data_source_id,
            },
        )

        try:
            snapshot = self.github.fetch_all()
        except Exception:
            LOGGER.exception("GitHub fetch failed")
            return True

        repo_pages, repo_incomplete = self.notion.query_all_pages(repo_context)
        issue_pages, issue_incomplete = self.notion.query_all_pages(issue_context)
        pr_pages, pr_incomplete = self.notion.query_all_pages(pr_context)
        if repo_incomplete or issue_incomplete or pr_incomplete:
            LOGGER.error("Notion query incomplete; skipping all writes")
            return True

        state = SyncState()
        repo_index = index_pages_by_github_id(repo_pages, repo_context.name)
        issue_index = index_pages_by_github_id(issue_pages, issue_context.name)
        pr_index = index_pages_by_github_id(pr_pages, pr_context.name)
        for github_id, page in repo_index.items():
            state.repo_page_by_github_id[github_id] = page["id"]
        for github_id, page in issue_index.items():
            state.issue_page_by_github_id[github_id] = page["id"]

        repo_stats = self._sync_repositories(repo_context, snapshot.repositories, repo_index, state)
        issue_stats = self._sync_issues(issue_context, snapshot, issue_index, state)
        pr_stats = self._sync_pull_requests(pr_context, snapshot, pr_index, state)

        for name, stats in (
            ("RepositoryDB", repo_stats),
            ("IssueDB", issue_stats),
            ("PullRequestDB", pr_stats),
        ):
            LOGGER.info(
                "%s: created=%s updated=%s unchanged=%s failed=%s",
                name,
                stats.created,
                stats.updated,
                stats.unchanged,
                stats.failed,
            )
        return state.failures

    def _sync_repositories(
        self,
        context: DatabaseContext,
        records: list[RepositoryRecord],
        existing: dict[str, dict[str, Any]],
        state: SyncState,
    ) -> WriteStats:
        stats = WriteStats()
        for record in records:
            desired = build_repository_properties(record)
            page = existing.get(record.github_id)
            try:
                if page is None:
                    created = self.notion.create_page(context, desired)
                    state.repo_page_by_github_id[record.github_id] = created["id"]
                    stats.created += 1
                elif repository_needs_update(page, desired):
                    self.notion.update_page(page["id"], desired)
                    state.repo_page_by_github_id[record.github_id] = page["id"]
                    stats.updated += 1
                else:
                    state.repo_page_by_github_id[record.github_id] = page["id"]
                    stats.unchanged += 1
            except Exception:
                LOGGER.exception("Failed to sync repository %s", record.name_with_owner)
                stats.failed += 1
                state.failures = True
        return stats

    def _sync_issues(
        self,
        context: DatabaseContext,
        snapshot: GitHubSnapshot,
        existing: dict[str, dict[str, Any]],
        state: SyncState,
    ) -> WriteStats:
        stats = WriteStats()
        for record in snapshot.issues:
            repo_page_id = state.repo_page_by_github_id.get(record.repository_github_id)
            if not repo_page_id:
                LOGGER.error(
                    "Skipping issue %s: repository page missing",
                    record.url,
                )
                stats.failed += 1
                state.failures = True
                continue
            status = issue_status_label(record.state, record.state_reason)
            desired = build_issue_properties(record, repo_page_id, status)
            page = existing.get(record.github_id)
            try:
                if page is None:
                    created = self.notion.create_page(context, desired)
                    state.issue_page_by_github_id[record.github_id] = created["id"]
                    stats.created += 1
                elif issue_or_pr_needs_update(page, desired):
                    self.notion.update_page(page["id"], desired)
                    state.issue_page_by_github_id[record.github_id] = page["id"]
                    stats.updated += 1
                else:
                    state.issue_page_by_github_id[record.github_id] = page["id"]
                    stats.unchanged += 1
            except Exception:
                LOGGER.exception("Failed to sync issue %s", record.url)
                stats.failed += 1
                state.failures = True
        return stats

    def _sync_pull_requests(
        self,
        context: DatabaseContext,
        snapshot: GitHubSnapshot,
        existing: dict[str, dict[str, Any]],
        state: SyncState,
    ) -> WriteStats:
        stats = WriteStats()
        issue_key_to_github_id = {
            (issue.repo_name_with_owner.lower(), issue.number): issue.github_id
            for issue in snapshot.issues
        }
        for record in snapshot.pull_requests:
            repo_page_id = state.repo_page_by_github_id.get(record.repository_github_id)
            if not repo_page_id:
                LOGGER.error(
                    "Skipping pull request %s: repository page missing",
                    record.url,
                )
                stats.failed += 1
                state.failures = True
                continue
            linked_issue_ids = resolve_issue_github_ids(record, issue_key_to_github_id)
            issue_page_ids = sorted(
                state.issue_page_by_github_id[issue_id]
                for issue_id in linked_issue_ids
                if issue_id in state.issue_page_by_github_id
            )
            status = pull_request_status_label(record.state)
            desired = build_pull_request_properties(record, repo_page_id, issue_page_ids, status)
            page = existing.get(record.github_id)
            try:
                if page is None:
                    self.notion.create_page(context, desired)
                    stats.created += 1
                elif issue_or_pr_needs_update(page, desired, "Issues"):
                    self.notion.update_page(page["id"], desired)
                    stats.updated += 1
                else:
                    stats.unchanged += 1
            except Exception:
                LOGGER.exception("Failed to sync pull request %s", record.url)
                stats.failed += 1
                state.failures = True
        return stats

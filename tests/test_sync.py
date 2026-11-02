"""Integration-style tests for Notion sync with mocked HTTP."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from github_notion_sync.config import Settings
from github_notion_sync.github import GitHubClientImpl
from github_notion_sync.notion import (
    ISSUE_SCHEMA,
    PULL_REQUEST_SCHEMA,
    REPOSITORY_SCHEMA,
    NotionClientImpl,
)
from github_notion_sync.sync import (
    SyncServiceImpl,
    datetime_minute_key,
    issue_or_pr_needs_update,
    repository_needs_update,
)


@pytest.fixture
def settings():
    return Settings("gh", "nt", "repo-db", "issue-db", "pr-db")


def notion_properties(
    *,
    title: str = "Title",
    number: int | None = None,
    status: str | None = None,
    url: str = "https://example.com",
    created: str = "2026-01-01T00:00:00Z",
    updated: str = "2026-01-01T00:00:00Z",
    github_id: str = "GHID",
    repository_ids: list[str] | None = None,
    issue_ids: list[str] | None = None,
    relation_has_more: dict[str, bool] | None = None,
) -> dict[str, Any]:
    relation_has_more = relation_has_more or {}
    props: dict[str, Any] = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
        "URL": {"type": "url", "url": url},
        "Created": {"type": "date", "date": {"start": created}},
        "Updated": {"type": "date", "date": {"start": updated}},
        "GitHub ID": {"type": "rich_text", "rich_text": [{"plain_text": github_id}]},
    }
    if number is not None:
        props["Number"] = {"type": "number", "number": number}
    if status is not None:
        props["Status"] = {"type": "select", "select": {"name": status}}
    if repository_ids is not None:
        props["Repository"] = {
            "type": "relation",
            "relation": [{"id": page_id} for page_id in repository_ids],
            "has_more": relation_has_more.get("Repository", False),
        }
    if issue_ids is not None:
        props["Issues"] = {
            "type": "relation",
            "relation": [{"id": page_id} for page_id in issue_ids],
            "has_more": relation_has_more.get("Issues", False),
        }
    return props


def schema_properties(context: str) -> dict[str, Any]:
    if context == "repo":
        return {
            "Name": {"type": "title"},
            "URL": {"type": "url"},
            "Created": {"type": "date"},
            "Updated": {"type": "date"},
            "GitHub ID": {"type": "rich_text"},
        }
    if context == "issue":
        return {
            **schema_properties("repo"),
            "Number": {"type": "number"},
            "Status": {"type": "select"},
            "Repository": {
                "type": "relation",
                "relation": {"data_source_id": "repo-source"},
            },
        }
    return {
        **schema_properties("issue"),
        "Issues": {
            "type": "relation",
            "relation": {"data_source_id": "issue-source"},
        },
    }


def github_graphql_response() -> dict[str, Any]:
    return {
        "data": {
            "viewer": {
                "repositories": {
                    "nodes": [
                        {
                            "id": "REPO_GH",
                            "name": "repo",
                            "nameWithOwner": "owner/repo",
                            "url": "https://github.com/owner/repo",
                            "createdAt": "2026-01-01T00:00:00Z",
                            "pushedAt": "2026-01-02T00:00:00Z",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }


def github_issues_response() -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {
                    "nodes": [
                        {
                            "id": "ISSUE_GH",
                            "number": 3,
                            "title": "Issue title",
                            "url": "https://github.com/owner/repo/issues/3",
                            "state": "OPEN",
                            "stateReason": None,
                            "createdAt": "2026-01-01T00:00:00Z",
                            "updatedAt": "2026-01-01T00:00:00Z",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }


def github_prs_response() -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "id": "PR_GH",
                            "number": 4,
                            "title": "PR title",
                            "url": "https://github.com/owner/repo/pull/4",
                            "state": "OPEN",
                            "body": "Closes #3",
                            "createdAt": "2026-01-01T00:00:00Z",
                            "updatedAt": "2026-01-01T00:00:00Z",
                            "closingIssuesReferences": {
                                "totalCount": 0,
                                "nodes": [],
                            },
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }


class NotionState:
    def __init__(self) -> None:
        self.pages: dict[str, list[dict[str, Any]]] = {
            "repo-source": [],
            "issue-source": [],
            "pr-source": [],
        }
        self.post_calls = 0
        self.patch_calls = 0
        self.next_id = 1
        self.post_pages_attempts = 0
        self.fail_first_post_5xx = False
        self.patched_page_ids: list[str] = []
        self.fail_patch_page_ids: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        if path.startswith("databases/"):
            db_id = path.split("/", 1)[1]
            source = {
                "repo-db": "repo-source",
                "issue-db": "issue-source",
                "pr-db": "pr-source",
            }[db_id]
            return httpx.Response(200, json={"data_sources": [{"id": source}], "title": []})
        if path.startswith("data_sources/") and path.endswith("/query"):
            source = path.split("/")[1]
            return httpx.Response(
                200,
                json={
                    "results": self.pages[source],
                    "has_more": False,
                    "request_status": {"type": "complete"},
                },
            )
        if path.startswith("data_sources/"):
            source = path.split("/")[1]
            context = {
                "repo-source": "repo",
                "issue-source": "issue",
                "pr-source": "pr",
            }[source]
            return httpx.Response(200, json={"properties": schema_properties(context)})
        if path == "pages" and request.method == "POST":
            self.post_calls += 1
            self.post_pages_attempts += 1
            if self.post_pages_attempts == 1 and self.fail_first_post_5xx:
                return httpx.Response(500)
            page_id = f"page-{self.next_id}"
            self.next_id += 1
            payload = json.loads(request.content)
            source = payload["parent"]["data_source_id"]
            page = {
                "object": "page",
                "id": page_id,
                "created_time": "2026-01-01T00:00:00Z",
                "properties": self._properties_from_write(payload["properties"]),
            }
            self.pages[source].append(page)
            return httpx.Response(200, json=page)
        if path.startswith("pages/") and request.method == "PATCH":
            page_id = path.split("/", 1)[1]
            if page_id in self.fail_patch_page_ids:
                return httpx.Response(500)
            self.patch_calls += 1
            self.patched_page_ids.append(page_id)
            payload = json.loads(request.content)
            for pages in self.pages.values():
                for page in pages:
                    if page["id"] == page_id:
                        page["properties"] = self._properties_from_write(payload["properties"])
            return httpx.Response(200, json={"id": page_id})
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    def _properties_from_write(self, written: dict[str, Any]) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for key, value in written.items():
            if key == "Name":
                props[key] = {
                    "type": "title",
                    "title": [{"plain_text": value["title"][0]["text"]["content"]}],
                }
            elif key == "Number":
                props[key] = {"type": "number", "number": value["number"]}
            elif key == "Status":
                props[key] = {"type": "select", "select": value["select"]}
            elif key == "URL":
                props[key] = {"type": "url", "url": value["url"]}
            elif key in ("Created", "Updated"):
                date = value.get("date")
                props[key] = {
                    "type": "date",
                    "date": {"start": date["start"]} if date else None,
                }
            elif key == "GitHub ID":
                props[key] = {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": value["rich_text"][0]["text"]["content"]}],
                }
            elif key in ("Repository", "Issues"):
                props[key] = {
                    "type": "relation",
                    "relation": value["relation"],
                    "has_more": False,
                }
        return props


def build_clients(settings: Settings, notion_state: NotionState, github_handler):
    notion_client = httpx.Client(transport=httpx.MockTransport(notion_state.handler))
    github_client = httpx.Client(transport=httpx.MockTransport(github_handler))
    return (
        SyncServiceImpl(
            settings,
            NotionClientImpl(settings.notion_token, notion_client),
            GitHubClientImpl(settings.github_token, github_client),
        ),
        notion_client,
        github_client,
    )


def default_github_handler(request: httpx.Request) -> httpx.Response:
    query = json.loads(request.content)["query"]
    if "viewer" in query:
        return httpx.Response(200, json=github_graphql_response())
    if "issues(" in query:
        return httpx.Response(200, json=github_issues_response())
    return httpx.Response(200, json=github_prs_response())


def test_full_sync_unchanged_on_second_run(settings):
    state = NotionState()
    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    assert service.run() is False
    assert state.post_calls == 3
    assert state.patch_calls == 0
    assert service.run() is False
    assert state.post_calls == 3
    assert state.patch_calls == 0
    notion_client.close()
    github_client.close()


def test_full_sync_creates_and_links(settings):
    state = NotionState()
    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    assert service.run() is False
    assert state.post_calls == 3
    assert state.patch_calls == 0
    pr_page = state.pages["pr-source"][0]
    issue_ids = [item["id"] for item in pr_page["properties"]["Issues"]["relation"]]
    assert issue_ids == [state.pages["issue-source"][0]["id"]]
    notion_client.close()
    github_client.close()


def test_date_minute_normalization():
    assert datetime_minute_key("2026-01-01T00:00:00Z") == datetime_minute_key(
        "2026-01-01T00:00:01+00:00"
    )


def test_repository_unchanged_when_only_seconds_differ(settings):
    from github_notion_sync.github import RepositoryRecord
    from github_notion_sync.sync import build_repository_properties

    record = RepositoryRecord(
        github_id="REPO_GH",
        name="repo",
        name_with_owner="owner/repo",
        url="https://github.com/owner/repo",
        created_at="2026-01-01T00:00:00Z",
        pushed_at="2026-01-02T00:00:05Z",
    )
    desired = build_repository_properties(record)
    page = {
        "properties": notion_properties(
            title="repo",
            url=record.url,
            created="2026-01-01T00:00:00.000Z",
            updated="2026-01-02T00:00:59Z",
            github_id="REPO_GH",
        )
    }
    assert repository_needs_update(page, desired) is False


def test_relation_has_more_triggers_update(settings):
    from github_notion_sync.github import IssueRecord
    from github_notion_sync.sync import build_issue_properties

    record = IssueRecord(
        github_id="ISSUE_GH",
        number=3,
        title="Issue title",
        url="https://github.com/owner/repo/issues/3",
        state="OPEN",
        state_reason=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        repository_github_id="REPO_GH",
        repo_name_with_owner="owner/repo",
    )
    desired = build_issue_properties(record, "repo-page", "Open")
    page = {
        "properties": notion_properties(
            title=record.title,
            number=3,
            status="Open",
            url=record.url,
            github_id="ISSUE_GH",
            repository_ids=["repo-page"],
            relation_has_more={"Repository": True},
        )
    }
    assert issue_or_pr_needs_update(page, desired) is True


def test_incomplete_query_skips_writes(settings):
    state = NotionState()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        if path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [],
                    "has_more": False,
                    "request_status": {"type": "incomplete", "incomplete_reason": "x"},
                },
            )
        return state.handler(request)

    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    notion_client.close()
    notion_client = httpx.Client(transport=httpx.MockTransport(handler))
    service.notion.client = notion_client
    assert service.run() is True
    assert state.post_calls == 0
    assert state.patch_calls == 0
    notion_client.close()
    github_client.close()


def test_duplicate_github_id_warning(settings, caplog):
    state = NotionState()
    state.pages["repo-source"] = [
        {
            "object": "page",
            "id": "old",
            "created_time": "2026-01-01T00:00:00Z",
            "properties": notion_properties(title="old", github_id="REPO_GH"),
        },
        {
            "object": "page",
            "id": "new",
            "created_time": "2026-01-02T00:00:00Z",
            "properties": notion_properties(title="new", github_id="REPO_GH"),
        },
    ]
    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    with caplog.at_level(logging.WARNING):
        service.run()
    assert any("duplicate GitHub ID" in record.message for record in caplog.records)
    assert state.patch_calls == 1
    assert state.patched_page_ids == ["old"]
    new_page = next(page for page in state.pages["repo-source"] if page["id"] == "new")
    assert new_page["properties"]["Name"]["title"][0]["plain_text"] == "new"
    notion_client.close()
    github_client.close()


def test_issue_update_failure_keeps_pr_issue_relation(settings):
    state = NotionState()
    repo_page = {
        "object": "page",
        "id": "repo-page",
        "created_time": "2026-01-01T00:00:00Z",
        "properties": notion_properties(
            title="repo",
            url="https://github.com/owner/repo",
            github_id="REPO_GH",
        ),
    }
    issue_page = {
        "object": "page",
        "id": "issue-page",
        "created_time": "2026-01-01T00:00:00Z",
        "properties": notion_properties(
            title="stale title",
            number=3,
            status="Open",
            url="https://github.com/owner/repo/issues/3",
            github_id="ISSUE_GH",
            repository_ids=["repo-page"],
        ),
    }
    pr_page = {
        "object": "page",
        "id": "pr-page",
        "created_time": "2026-01-01T00:00:00Z",
        "properties": notion_properties(
            title="PR title",
            number=4,
            status="Open",
            url="https://github.com/owner/repo/pull/4",
            github_id="PR_GH",
            repository_ids=["repo-page"],
            issue_ids=["issue-page"],
        ),
    }
    state.pages["repo-source"] = [repo_page]
    state.pages["issue-source"] = [issue_page]
    state.pages["pr-source"] = [pr_page]
    state.fail_patch_page_ids = {"issue-page"}
    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    assert service.run() is True
    assert "pr-page" not in state.patched_page_ids
    pr_issues = pr_page["properties"]["Issues"]["relation"]
    assert [item["id"] for item in pr_issues] == ["issue-page"]
    notion_client.close()
    github_client.close()


def test_post_pages_429_retries(settings, monkeypatch):
    state = NotionState()
    sleeps: list[float] = []
    monkeypatch.setattr("github_notion_sync.notion.time.sleep", lambda value: sleeps.append(value))
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        if path == "pages" and request.method == "POST":
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
        return state.handler(request)

    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    notion_client.close()
    notion_client = httpx.Client(transport=httpx.MockTransport(handler))
    service.notion.client = notion_client
    assert service.run() is False
    assert attempts["count"] >= 2
    notion_client.close()
    github_client.close()


def test_post_pages_5xx_no_retry(settings):
    state = NotionState()
    state.fail_first_post_5xx = True
    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    assert service.run() is True
    assert state.post_pages_attempts == 1
    notion_client.close()
    github_client.close()


def test_repository_create_failure_skips_children(settings):
    state = NotionState()
    state.fail_first_post_5xx = True
    service, notion_client, github_client = build_clients(settings, state, default_github_handler)
    assert service.run() is True
    assert state.post_calls == 1
    notion_client.close()
    github_client.close()


def test_schema_validation_missing_property(settings):
    notion = NotionClientImpl("token", httpx.Client())
    notion.resolve_data_source_id = lambda _db: "repo-source"  # type: ignore[method-assign]
    props = schema_properties("repo")
    props.pop("URL")
    notion.request = lambda method, path, **kwargs: {"properties": props}  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="RepositoryDB: property URL must be url"):
        notion.validate_database(
            "RepositoryDB", settings.repository_database_id, REPOSITORY_SCHEMA, {}
        )


def test_schema_validation_wrong_type(settings):
    notion = NotionClientImpl("token", httpx.Client())
    notion.resolve_data_source_id = lambda _db: "issue-source"  # type: ignore[method-assign]
    props = schema_properties("issue")
    props["Status"] = {"type": "rich_text"}
    notion.request = lambda method, path, **kwargs: {"properties": props}  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="IssueDB: property Status must be select"):
        notion.validate_database(
            "IssueDB",
            settings.issue_database_id,
            ISSUE_SCHEMA,
            {"Repository": "repo-source"},
        )


def test_schema_validation_wrong_relation(settings):
    notion = NotionClientImpl("token", httpx.Client())
    notion.resolve_data_source_id = lambda _db: "pr-source"  # type: ignore[method-assign]
    props = schema_properties("pr")
    props["Issues"]["relation"]["data_source_id"] = "wrong"
    notion.request = lambda method, path, **kwargs: {"properties": props}  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="PullRequestDB: property Issues must relate to"):
        notion.validate_database(
            "PullRequestDB",
            settings.pull_request_database_id,
            PULL_REQUEST_SCHEMA,
            {"Repository": "repo-source", "Issues": "issue-source"},
        )


def test_graphql_errors_skip_notion_writes(settings):
    state = NotionState()

    def github_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}, "errors": [{"message": "fail"}]})

    service, notion_client, github_client = build_clients(settings, state, github_handler)
    assert service.run() is True
    assert state.post_calls == 0
    notion_client.close()
    github_client.close()

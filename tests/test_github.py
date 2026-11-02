"""Unit tests for GitHub parsing and status mapping."""

import json

import httpx
import pytest

from github_notion_sync.github import (
    GitHubClientImpl,
    PullRequestRecord,
    issue_status_label,
    parse_closing_references,
    pull_request_status_label,
    resolve_issue_github_ids,
)


@pytest.mark.parametrize(
    ("state", "state_reason", "expected"),
    [
        ("OPEN", None, "Open"),
        ("CLOSED", "COMPLETED", "Closed"),
        ("CLOSED", "NOT_PLANNED", "Closed as not planned"),
        ("CLOSED", "DUPLICATE", "Closed as duplicate"),
        ("CLOSED", "REOPENED", "Closed"),
        ("CLOSED", None, "Closed"),
    ],
)
def test_issue_status_label(state, state_reason, expected):
    assert issue_status_label(state, state_reason) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [("OPEN", "Open"), ("MERGED", "Merged"), ("CLOSED", "Closed")],
)
def test_pull_request_status_label(state, expected):
    assert pull_request_status_label(state) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Closes #3", {("owner/repo", 3)}),
        ("fixes: #4", {("owner/repo", 4)}),
        ("Resolved Owner/Repo#5", {("owner/repo", 5)}),
        ("close https://github.com/owner/repo/issues/6", {("owner/repo", 6)}),
    ],
)
def test_parse_closing_references_picks(body, expected):
    refs = parse_closing_references(body, "owner/repo")
    assert refs == expected


@pytest.mark.parametrize("body", ["See #7", "Mentioned #8 in passing", "#8"])
def test_parse_closing_references_ignores(body):
    assert parse_closing_references(body, "owner/repo") == set()


def test_resolve_issue_github_ids_union():
    pr = PullRequestRecord(
        github_id="PR_1",
        number=4,
        title="PR",
        url="https://example.com/pull/4",
        state="OPEN",
        body="Closes #3",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        repository_github_id="REPO_1",
        repo_name_with_owner="owner/repo",
        closing_issue_ids=frozenset({"ISSUE_FROM_API"}),
    )
    issue_key_to_github_id = {("owner/repo", 3): "ISSUE_FROM_BODY"}
    ids = resolve_issue_github_ids(pr, issue_key_to_github_id)
    assert ids == {"ISSUE_FROM_API", "ISSUE_FROM_BODY"}


def test_resolve_issue_github_ids_excludes_unknown():
    pr = PullRequestRecord(
        github_id="PR_1",
        number=1,
        title="PR",
        url="https://example.com/pull/1",
        state="OPEN",
        body="Closes #99",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        repository_github_id="REPO_1",
        repo_name_with_owner="owner/repo",
    )
    assert resolve_issue_github_ids(pr, {}) == set()


def test_github_pagination_and_graphql_errors():
    calls: list[str] = []

    def handler(request):
        body = json.loads(request.content)
        query = body["query"]
        if "viewer" in query:
            calls.append("repos")
            if body["variables"].get("cursor") is None:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "viewer": {
                                "repositories": {
                                    "nodes": [
                                        {
                                            "id": "R1",
                                            "name": "repo",
                                            "nameWithOwner": "o/r",
                                            "url": "https://github.com/o/r",
                                            "createdAt": "2026-01-01T00:00:00Z",
                                            "pushedAt": "2026-01-02T00:00:00Z",
                                        }
                                    ],
                                    "pageInfo": {"hasNextPage": True, "endCursor": "rc"},
                                }
                            }
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "viewer": {
                            "repositories": {
                                "nodes": [
                                    {
                                        "id": "R2",
                                        "name": "repo2",
                                        "nameWithOwner": "o/r2",
                                        "url": "https://github.com/o/r2",
                                        "createdAt": "2026-01-01T00:00:00Z",
                                        "pushedAt": None,
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                },
            )
        if "issues(" in query:
            calls.append("issues")
            if body["variables"].get("cursor") is None:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "repository": {
                                "issues": {
                                    "nodes": [
                                        {
                                            "id": "I1",
                                            "number": 1,
                                            "title": "Issue",
                                            "url": "https://github.com/o/r/issues/1",
                                            "state": "OPEN",
                                            "stateReason": None,
                                            "createdAt": "2026-01-01T00:00:00Z",
                                            "updatedAt": "2026-01-01T00:00:00Z",
                                        }
                                    ],
                                    "pageInfo": {"hasNextPage": True, "endCursor": "ic"},
                                }
                            }
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "I2",
                                        "number": 2,
                                        "title": "Issue 2",
                                        "url": "https://github.com/o/r/issues/2",
                                        "state": "CLOSED",
                                        "stateReason": "COMPLETED",
                                        "createdAt": "2026-01-01T00:00:00Z",
                                        "updatedAt": "2026-01-02T00:00:00Z",
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                },
            )
        calls.append("prs")
        if body["variables"].get("cursor") is None:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequests": {
                                "nodes": [
                                    {
                                        "id": "P1",
                                        "number": 1,
                                        "title": "PR",
                                        "url": "https://github.com/o/r/pull/1",
                                        "state": "OPEN",
                                        "body": "",
                                        "createdAt": "2026-01-01T00:00:00Z",
                                        "updatedAt": "2026-01-01T00:00:00Z",
                                        "closingIssuesReferences": {"totalCount": 0, "nodes": []},
                                    }
                                ],
                                "pageInfo": {"hasNextPage": True, "endCursor": "pc"},
                            }
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "id": "P2",
                                    "number": 2,
                                    "title": "PR 2",
                                    "url": "https://github.com/o/r/pull/2",
                                    "state": "MERGED",
                                    "body": None,
                                    "createdAt": "2026-01-01T00:00:00Z",
                                    "updatedAt": "2026-01-02T00:00:00Z",
                                    "closingIssuesReferences": {"totalCount": 0, "nodes": []},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = GitHubClientImpl("token", client).fetch_all()
    assert len(snapshot.repositories) == 2
    assert len(snapshot.issues) == 4
    assert len(snapshot.pull_requests) == 4
    assert calls.count("repos") == 2
    assert calls.count("issues") == 4
    assert calls.count("prs") == 4


def test_github_graphql_errors_fail():
    def handler(_request):
        return httpx.Response(200, json={"data": {}, "errors": [{"message": "boom"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="GraphQL errors"):
            GitHubClientImpl("token", client).fetch_all()


def test_github_retries_after_5xx():
    calls = []

    def handler(_request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502)
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {
                        "repositories": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = GitHubClientImpl("token", client).fetch_all()
    assert snapshot.repositories == []
    assert len(calls) == 2

"""GitHub GraphQL fetch and closing-keyword parsing."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
MAX_ATTEMPTS = 5

REPOSITORIES_QUERY = """
query($cursor: String) {
  viewer {
    repositories(ownerAffiliations: OWNER, isFork: false, first: 100, after: $cursor) {
      nodes {
        id
        name
        nameWithOwner
        url
        createdAt
        pushedAt
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

ISSUES_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $cursor) {
      nodes {
        id
        number
        title
        url
        state
        stateReason
        createdAt
        updatedAt
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

PULL_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor) {
      nodes {
        id
        number
        title
        url
        state
        body
        createdAt
        updatedAt
        closingIssuesReferences(first: 25) {
          totalCount
          nodes {
            id
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

CLOSING_KEYWORD = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s*:?\s+"
    r"(?:"
    r"#(\d+)"
    r"|([\w.-]+)/([\w.-]+)#(\d+)"
    r"|https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)"
    r")"
)


@dataclass(frozen=True)
class RepositoryRecord:
    github_id: str
    name: str
    name_with_owner: str
    url: str
    created_at: str
    pushed_at: str | None


@dataclass(frozen=True)
class IssueRecord:
    github_id: str
    number: int
    title: str
    url: str
    state: str
    state_reason: str | None
    created_at: str
    updated_at: str
    repository_github_id: str
    repo_name_with_owner: str


@dataclass(frozen=True)
class PullRequestRecord:
    github_id: str
    number: int
    title: str
    url: str
    state: str
    body: str | None
    created_at: str
    updated_at: str
    repository_github_id: str
    repo_name_with_owner: str
    closing_issue_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class GitHubSnapshot:
    repositories: list[RepositoryRecord]
    issues: list[IssueRecord]
    pull_requests: list[PullRequestRecord]


def issue_status_label(state: str, state_reason: str | None) -> str:
    if state == "OPEN":
        return "Open"
    if state != "CLOSED":
        raise ValueError(f"Unexpected issue state: {state}")
    if state_reason == "NOT_PLANNED":
        return "Closed as not planned"
    if state_reason == "DUPLICATE":
        return "Closed as duplicate"
    return "Closed"


def pull_request_status_label(state: str) -> str:
    mapping = {"OPEN": "Open", "MERGED": "Merged", "CLOSED": "Closed"}
    if state not in mapping:
        raise ValueError(f"Unexpected pull request state: {state}")
    return mapping[state]


def parse_closing_references(body: str | None, repo_name_with_owner: str) -> set[tuple[str, int]]:
    """Return (lowercase nameWithOwner, issue number) pairs from closing keywords."""
    if not body:
        return set()
    repo_key = repo_name_with_owner.lower()
    refs: set[tuple[str, int]] = set()
    for match in CLOSING_KEYWORD.finditer(body):
        if match.group(1):
            refs.add((repo_key, int(match.group(1))))
        elif match.group(2) and match.group(3) and match.group(4):
            refs.add((match.group(2).lower() + "/" + match.group(3).lower(), int(match.group(4))))
        elif match.group(5) and match.group(6) and match.group(7):
            refs.add((match.group(5).lower() + "/" + match.group(6).lower(), int(match.group(7))))
    return refs


def resolve_issue_github_ids(
    pr: PullRequestRecord,
    issue_key_to_github_id: dict[tuple[str, int], str],
) -> set[str]:
    ids = set(pr.closing_issue_ids)
    for key in parse_closing_references(pr.body, pr.repo_name_with_owner):
        github_id = issue_key_to_github_id.get(key)
        if github_id:
            ids.add(github_id)
    return ids


class GitHubClientImpl:
    def __init__(self, token: str, client: httpx.Client) -> None:
        self.token = token
        self.client = client

    def fetch_all(self) -> GitHubSnapshot:
        repositories = self._fetch_repositories()
        issues: list[IssueRecord] = []
        pull_requests: list[PullRequestRecord] = []
        for repo in repositories:
            owner, name = repo.name_with_owner.split("/", 1)
            issues.extend(self._fetch_issues(owner, name, repo.github_id, repo.name_with_owner))
            pull_requests.extend(
                self._fetch_pull_requests(owner, name, repo.github_id, repo.name_with_owner)
            )
        return GitHubSnapshot(repositories=repositories, issues=issues, pull_requests=pull_requests)

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.client.post(
                    GITHUB_GRAPHQL_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code >= 500:
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
            if response.status_code in (403, 429) and response.headers.get("Retry-After"):
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(min(60, float(response.headers["Retry-After"])))
                    continue
            response.raise_for_status()
            body = response.json()
            if body.get("errors"):
                raise RuntimeError(f"GitHub GraphQL errors: {body['errors']}")
            return body["data"]
        raise RuntimeError("GitHub GraphQL retries exhausted")

    def _fetch_repositories(self) -> list[RepositoryRecord]:
        records: list[RepositoryRecord] = []
        cursor: str | None = None
        while True:
            data = self._graphql(REPOSITORIES_QUERY, {"cursor": cursor})
            connection = data["viewer"]["repositories"]
            for node in connection["nodes"]:
                records.append(
                    RepositoryRecord(
                        github_id=node["id"],
                        name=node["name"],
                        name_with_owner=node["nameWithOwner"],
                        url=node["url"],
                        created_at=node["createdAt"],
                        pushed_at=node.get("pushedAt"),
                    )
                )
            if not connection["pageInfo"]["hasNextPage"]:
                return records
            cursor = connection["pageInfo"]["endCursor"]

    def _fetch_issues(
        self, owner: str, name: str, repository_github_id: str, name_with_owner: str
    ) -> list[IssueRecord]:
        records: list[IssueRecord] = []
        cursor: str | None = None
        while True:
            data = self._graphql(ISSUES_QUERY, {"owner": owner, "name": name, "cursor": cursor})
            connection = data["repository"]["issues"]
            for node in connection["nodes"]:
                records.append(
                    IssueRecord(
                        github_id=node["id"],
                        number=node["number"],
                        title=node["title"],
                        url=node["url"],
                        state=node["state"],
                        state_reason=node.get("stateReason"),
                        created_at=node["createdAt"],
                        updated_at=node["updatedAt"],
                        repository_github_id=repository_github_id,
                        repo_name_with_owner=name_with_owner,
                    )
                )
            if not connection["pageInfo"]["hasNextPage"]:
                return records
            cursor = connection["pageInfo"]["endCursor"]

    def _fetch_pull_requests(
        self, owner: str, name: str, repository_github_id: str, name_with_owner: str
    ) -> list[PullRequestRecord]:
        records: list[PullRequestRecord] = []
        cursor: str | None = None
        while True:
            data = self._graphql(
                PULL_REQUESTS_QUERY, {"owner": owner, "name": name, "cursor": cursor}
            )
            connection = data["repository"]["pullRequests"]
            for node in connection["nodes"]:
                closing = node["closingIssuesReferences"]
                if closing["totalCount"] > 25:
                    LOGGER.warning(
                        "PR %s has %s closing issue references; using first 25 only",
                        node["url"],
                        closing["totalCount"],
                    )
                closing_ids = frozenset(item["id"] for item in closing["nodes"])
                records.append(
                    PullRequestRecord(
                        github_id=node["id"],
                        number=node["number"],
                        title=node["title"],
                        url=node["url"],
                        state=node["state"],
                        body=node.get("body"),
                        created_at=node["createdAt"],
                        updated_at=node["updatedAt"],
                        repository_github_id=repository_github_id,
                        repo_name_with_owner=name_with_owner,
                        closing_issue_ids=closing_ids,
                    )
                )
            if not connection["pageInfo"]["hasNextPage"]:
                return records
            cursor = connection["pageInfo"]["endCursor"]

"""Notion transport, schema validation, and page CRUD."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

NOTION_VERSION = "2025-09-03"
MAX_ATTEMPTS = 5

REPOSITORY_SCHEMA: dict[str, str] = {
    "Name": "title",
    "URL": "url",
    "Created": "date",
    "Updated": "date",
    "GitHub ID": "rich_text",
}

ISSUE_SCHEMA: dict[str, str] = {
    "Name": "title",
    "Number": "number",
    "Status": "status",
    "URL": "url",
    "Created": "date",
    "Updated": "date",
    "Repository": "relation",
    "GitHub ID": "rich_text",
}

PULL_REQUEST_SCHEMA: dict[str, str] = {
    **ISSUE_SCHEMA,
    "Issues": "relation",
}


def read_rich_text(items: list[dict[str, Any]]) -> str:
    return "".join(
        item.get("plain_text", item.get("text", {}).get("content", "")) for item in items
    )


@dataclass
class DatabaseContext:
    name: str
    database_id: str
    data_source_id: str
    schema: dict[str, str]
    relation_targets: dict[str, str]


class NotionClientImpl:
    def __init__(self, token: str, client: httpx.Client) -> None:
        self.token = token
        self.client = client
        self.last_request = 0.0

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        is_create_page = method == "POST" and path == "pages"
        for attempt in range(MAX_ATTEMPTS):
            time.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.request(
                    method,
                    "https://api.notion.com/v1/" + path,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Notion-Version": NOTION_VERSION,
                    },
                    **kwargs,
                )
            except httpx.TransportError:
                if is_create_page or attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if is_create_page and response.status_code >= 500:
                    response.raise_for_status()
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(min(60, float(response.headers.get("Retry-After", 2**attempt))))
                    continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Notion retries exhausted")

    def resolve_data_source_id(self, database_id: str) -> str:
        database = self.request("GET", f"databases/{database_id}")
        sources = [source["id"] for source in database.get("data_sources", [])]
        if len(sources) != 1:
            title = read_rich_text(database.get("title", [])) or database_id
            raise ValueError(
                f"Database {title} must have exactly one data source; found {len(sources)}"
            )
        return sources[0]

    def validate_database(
        self,
        display_name: str,
        database_id: str,
        expected: dict[str, str],
        relation_targets: dict[str, str],
    ) -> DatabaseContext:
        data_source_id = self.resolve_data_source_id(database_id)
        properties = self.request("GET", f"data_sources/{data_source_id}")["properties"]
        for prop_name, prop_type in expected.items():
            actual = properties.get(prop_name)
            if not actual or actual.get("type") != prop_type:
                raise ValueError(f"{display_name}: property {prop_name} must be {prop_type}")
            if prop_type == "relation":
                target = relation_targets[prop_name]
                relation = actual["relation"]
                actual_target = relation.get("data_source_id") or relation.get("database_id")
                if actual_target != target:
                    raise ValueError(
                        f"{display_name}: property {prop_name} must relate to {target}"
                    )
        return DatabaseContext(
            name=display_name,
            database_id=database_id,
            data_source_id=data_source_id,
            schema=expected,
            relation_targets=relation_targets,
        )

    def query_all_pages(self, context: DatabaseContext) -> tuple[list[dict[str, Any]], bool]:
        """Return all pages and whether any query was incomplete."""
        pages: list[dict[str, Any]] = []
        incomplete = False
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            result = self.request(
                "POST", f"data_sources/{context.data_source_id}/query", json=payload
            )
            status = result.get("request_status", {})
            if status.get("type") == "incomplete":
                LOGGER.warning(
                    "%s query incomplete: %s",
                    context.name,
                    status.get("incomplete_reason"),
                )
                incomplete = True
            for page in result.get("results", []):
                if page.get("object") == "page":
                    pages.append(page)
            if not result.get("has_more"):
                return pages, incomplete
            cursor = result["next_cursor"]

    def create_page(self, context: DatabaseContext, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            "pages",
            json={
                "parent": {"type": "data_source_id", "data_source_id": context.data_source_id},
                "properties": properties,
            },
        )

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"pages/{page_id}", json={"properties": properties})

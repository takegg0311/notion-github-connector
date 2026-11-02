"""Environment configuration, isolated from application logic."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    github_token: str
    notion_token: str
    repository_database_id: str
    issue_database_id: str
    pull_request_database_id: str

    def __post_init__(self) -> None:
        if not self.github_token:
            raise ValueError("GITHUB_TOKEN is required")
        if not self.notion_token:
            raise ValueError("NOTION_TOKEN is required")
        if not self.repository_database_id:
            raise ValueError("NOTION_REPOSITORY_DATABASE_ID is required")
        if not self.issue_database_id:
            raise ValueError("NOTION_ISSUE_DATABASE_ID is required")
        if not self.pull_request_database_id:
            raise ValueError("NOTION_PULL_REQUEST_DATABASE_ID is required")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            github_token=os.getenv("GITHUB_TOKEN", ""),
            notion_token=os.getenv("NOTION_TOKEN", ""),
            repository_database_id=os.getenv("NOTION_REPOSITORY_DATABASE_ID", ""),
            issue_database_id=os.getenv("NOTION_ISSUE_DATABASE_ID", ""),
            pull_request_database_id=os.getenv("NOTION_PULL_REQUEST_DATABASE_ID", ""),
        )

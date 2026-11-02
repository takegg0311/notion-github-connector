"""One-shot CLI suitable for cron and manual docker compose runs."""

import logging

import httpx
from dotenv import load_dotenv

from .config import Settings
from .github import GitHubClientImpl
from .notion import NotionClientImpl
from .sync import SyncServiceImpl


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    try:
        settings = Settings.from_env()
        with httpx.Client(timeout=60) as client:
            failures = SyncServiceImpl(
                settings,
                NotionClientImpl(settings.notion_token, client),
                GitHubClientImpl(settings.github_token, client),
            ).run()
        raise SystemExit(1 if failures else 0)
    except Exception:
        logging.getLogger(__name__).exception("Batch initialization failed")
        raise SystemExit(1)

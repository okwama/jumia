"""Env-backed settings. See env.example for the full set of variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GOOGLE_FEED_API_ENDPOINT = os.environ.get("GOOGLE_FEED_API_ENDPOINT", "")
UPLOAD_TEMPLATE_PATH = os.environ.get("UPLOAD_TEMPLATE_PATH", "./data/Upload_Template.xlsx")
COMMISSION_SHEET_PATH = os.environ.get("COMMISSION_SHEET_PATH", "./data/commission_and_shipping.xlsx")
JUMIA_GUIDELINES_PATH = os.environ.get("JUMIA_GUIDELINES_PATH", "./data/jumia-guidleines.xlsx")
DB_PATH = os.environ.get("DB_PATH", "./data/jumia_feed_sync.db")
IMAGE_CACHE_MAX_AGE_HOURS = float(os.environ.get("IMAGE_CACHE_MAX_AGE_HOURS", "24"))


def ensure_db_parent() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

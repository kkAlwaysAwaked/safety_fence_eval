"""Runtime config. Values come from environment / .env — never hardcode keys."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing {name}. Copy .env.example to .env and fill in your API keys."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw)


FENCE_API_KEY = _require("FENCE_API_KEY")
FENCE_BASE_URL = os.getenv(
    "FENCE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
FENCE_MODEL = os.getenv("FENCE_MODEL", "qwen3.5-flash")

DEEPSEEK_API_KEY = _require("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

FENCE_TIMEOUT = _int("FENCE_TIMEOUT", 60)
CLOUD_TIMEOUT = _int("CLOUD_TIMEOUT", 60)
CONCURRENCY = _int("CONCURRENCY", 30)

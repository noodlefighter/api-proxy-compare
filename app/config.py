from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    数据库路径: Path = BASE_DIR / "data.sqlite3"
    主机: str = os.getenv("APP_HOST", "127.0.0.1")
    端口: int = int(os.getenv("APP_PORT", "8000"))
    调度间隔秒: int = int(os.getenv("APP_SCRAPE_INTERVAL", "3600"))
    模型倍率换算系数: float = float(os.getenv("APP_MODEL_RATIO_SCALE", "2"))
    模型默认过滤关键词: tuple[str, ...] = tuple(
        part.strip()
        for part in os.getenv("APP_MODEL_FILTER", "Claude,GPT,Gemini").split(",")
        if part.strip()
    )


settings = Settings()

"""运行配置：端口、跨域、运行环境与持久化迁移参数。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_db_path() -> str:
    """持久化数据库默认放在 backend/data 下，可用 APP_DB_PATH 覆盖（空库核对时指向临时库）。"""
    path = Path(__file__).resolve().parent.parent / "data" / "ops_persist.sqlite3"
    return os.environ.get("APP_DB_PATH", str(path))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    app_name: str = "智能运营决策分析平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 持久化看板归因明细数据库位置；迁移批次大小与试运行上限也允许通过环境变量调节。
    db_path: str = field(default_factory=_default_db_path)
    migration_batch_size: int = field(default_factory=lambda: _env_int("APP_MIGRATION_BATCH_SIZE", 2))
    migration_max_batches: int = field(default_factory=lambda: _env_int("APP_MIGRATION_MAX_BATCHES", 0))


settings = Settings()

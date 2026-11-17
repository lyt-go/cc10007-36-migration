"""持久化数据仓库（SQLite）。

只承载本次迁移范围内的两个模块：运营看板 ``dashboard`` 与指标归因 ``metricdrill``。
其余模块仍走内存 Store，互不影响。

设计要点：
- ``biz_records`` 用「模块 + 业务编号 + 版本号」保存全部历史版本，
  另用部分唯一索引保证每个业务编号至多一个当前版本（is_current=1）；
- 每个迁移批次落 ``mig_batches``，每条记录绑定迁移运行号与批次号，
  看板视图与归因分析都从同一持久化批次读取，可按数据版本/批次回查；
- ``mig_cursor`` 记录分批迁移断点，服务重启后可从断点继续；
- ``mig_audit`` 留痕冻结期写入拦截与切换后的写入，保证只留存可追溯的持久化数据。

仅依赖标准库 sqlite3，克隆即可运行。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

MIGRATING_MODULES = ("dashboard", "metricdrill")

# 状态机：idle（未开始）→ migrating（分批迁移中，写入冻结）→ done（已切换，走持久库）
RUN_IDLE = "idle"
RUN_MIGRATING = "migrating"
RUN_DONE = "done"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mig_runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status              TEXT NOT NULL,
    batch_size          INTEGER NOT NULL,
    total_planned       INTEGER NOT NULL DEFAULT 0,
    total_migrated      INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    note                TEXT
);

CREATE TABLE IF NOT EXISTS mig_batches (
    run_id              INTEGER NOT NULL,
    batch_no            INTEGER NOT NULL,
    module              TEXT NOT NULL,
    offset_start        INTEGER NOT NULL,
    offset_end          INTEGER NOT NULL,
    row_count           INTEGER NOT NULL,
    content_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, batch_no, module)
);

CREATE TABLE IF NOT EXISTS mig_cursor (
    module              TEXT PRIMARY KEY,
    run_id              INTEGER NOT NULL,
    next_offset         INTEGER NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS biz_records (
    record_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    module              TEXT NOT NULL,
    biz_no              TEXT NOT NULL,
    version             INTEGER NOT NULL,
    is_current          INTEGER NOT NULL DEFAULT 0,
    row_id              INTEGER,
    payload_json        TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    run_id              INTEGER,
    batch_no            INTEGER,
    source_updated_at   TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (module, biz_no, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_biz_current
    ON biz_records(module, biz_no) WHERE is_current = 1;

CREATE INDEX IF NOT EXISTS idx_biz_module_current
    ON biz_records(module, is_current);

CREATE TABLE IF NOT EXISTS drill_links (
    link_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dashboard_biz_no    TEXT NOT NULL,
    drill_biz_no        TEXT NOT NULL,
    run_id              INTEGER NOT NULL,
    batch_no            INTEGER NOT NULL,
    contribution_rank   TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (dashboard_biz_no, drill_biz_no)
);

CREATE TABLE IF NOT EXISTS mig_audit (
    audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER,
    module              TEXT,
    biz_no              TEXT,
    action              TEXT NOT NULL,
    detail              TEXT,
    created_at          TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PersistentStore:
    """线程安全的 SQLite 封装：一把锁串行化所有写事务，读走同一连接的快照。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- 迁移运行 ----------------
    def insert_run(self, *, batch_size: int, total_planned: int, note: str = "") -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO mig_runs(status, batch_size, total_planned, started_at, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (RUN_MIGRATING, batch_size, total_planned, utc_now(), note),
            )
            return int(cur.lastrowid)

    def update_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        total_migrated: int | None = None,
        finished_at: str | None = None,
        note: str | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if total_migrated is not None:
            fields.append("total_migrated = ?")
            params.append(total_migrated)
        if finished_at is not None:
            fields.append("finished_at = ?")
            params.append(finished_at)
        if note is not None:
            fields.append("note = ?")
            params.append(note)
        if not fields:
            return
        params.append(run_id)
        with self.transaction() as conn:
            conn.execute(f"UPDATE mig_runs SET {', '.join(fields)} WHERE run_id = ?", params)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mig_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_latest_run(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mig_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM mig_runs ORDER BY run_id").fetchall()
            return [dict(row) for row in rows]

    # ---------------- 迁移断点 ----------------
    def get_cursor(self, module: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mig_cursor WHERE module = ?", (module,)
            ).fetchone()
            return dict(row) if row else None

    def all_cursors(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM mig_cursor ORDER BY module").fetchall()
            return [dict(row) for row in rows]

    def upsert_cursor(self, module: str, run_id: int, next_offset: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO mig_cursor(module, run_id, next_offset, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(module) DO UPDATE SET "
                "run_id = excluded.run_id, next_offset = excluded.next_offset, "
                "updated_at = excluded.updated_at",
                (module, run_id, next_offset, utc_now()),
            )

    # ---------------- 迁移批次 ----------------
    def insert_batch(
        self,
        *,
        run_id: int,
        batch_no: int,
        module: str,
        offset_start: int,
        offset_end: int,
        row_count: int,
        content_hash: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO mig_batches(run_id, batch_no, module, offset_start, offset_end, "
                "row_count, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, batch_no, module, offset_start, offset_end, row_count,
                 content_hash, utc_now()),
            )

    def list_batches(self, run_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if run_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM mig_batches ORDER BY run_id, batch_no, module"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM mig_batches WHERE run_id = ? ORDER BY batch_no, module",
                    (run_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def batch_exists(self, run_id: int, batch_no: int, module: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM mig_batches WHERE run_id = ? AND batch_no = ? AND module = ? LIMIT 1",
                (run_id, batch_no, module),
            ).fetchone()
            return row is not None

    # ---------------- 业务记录（版本化） ----------------
    def find_migrated_version(
        self, module: str, biz_no: str, content_hash: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM biz_records WHERE module = ? AND biz_no = ? AND content_hash = ? "
                "ORDER BY version DESC LIMIT 1",
                (module, biz_no, content_hash),
            ).fetchone()
            return dict(row) if row else None

    def latest_version(self, module: str, biz_no: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM biz_records WHERE module = ? AND biz_no = ?",
                (module, biz_no),
            ).fetchone()
            return int(row["v"])

    def insert_record(
        self,
        *,
        module: str,
        biz_no: str,
        version: int,
        is_current: bool,
        row_id: int | None,
        payload: dict[str, Any],
        content_hash: str,
        run_id: int | None,
        batch_no: int | None,
        source_updated_at: str | None,
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO biz_records(module, biz_no, version, is_current, row_id, "
                "payload_json, content_hash, run_id, batch_no, source_updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (module, biz_no, version, 1 if is_current else 0, row_id,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), content_hash,
                 run_id, batch_no, source_updated_at, utc_now()),
            )
            return int(cur.lastrowid)

    def clear_current(self, module: str, biz_no: str, *, exclude_record_id: int | None = None) -> None:
        with self.transaction() as conn:
            if exclude_record_id is None:
                conn.execute(
                    "UPDATE biz_records SET is_current = 0 WHERE module = ? AND biz_no = ?",
                    (module, biz_no),
                )
            else:
                conn.execute(
                    "UPDATE biz_records SET is_current = 0 WHERE module = ? AND biz_no = ? "
                    "AND record_id <> ?",
                    (module, biz_no, exclude_record_id),
                )

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM biz_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            return self._hydrate(row) if row else None

    def get_current_by_biz_no(self, module: str, biz_no: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM biz_records WHERE module = ? AND biz_no = ? AND is_current = 1",
                (module, biz_no),
            ).fetchone()
            return self._hydrate(row) if row else None

    def list_current(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM biz_records WHERE module = ? AND is_current = 1 "
                "ORDER BY record_id",
                (module,),
            ).fetchall()
            return [self._hydrate(row) for row in rows]

    def list_versions(self, module: str, biz_no: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM biz_records WHERE module = ? AND biz_no = ? ORDER BY version DESC",
                (module, biz_no),
            ).fetchall()
            return [self._hydrate(row) for row in rows]

    def count_current(self, module: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM biz_records WHERE module = ? AND is_current = 1",
                (module,),
            ).fetchone()
            return int(row["c"])

    def max_persistent_row_id(self, module: str) -> int:
        """切换后新写入沿用业务主键序列，避免与内存 id 撞号。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(row_id), 0) AS m FROM biz_records WHERE module = ?",
                (module,),
            ).fetchone()
            return int(row["m"])

    # ---------------- 看板 ↔ 归因关联 ----------------
    def upsert_link(
        self,
        *,
        dashboard_biz_no: str,
        drill_biz_no: str,
        run_id: int,
        batch_no: int,
        contribution_rank: str | None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO drill_links(dashboard_biz_no, drill_biz_no, run_id, batch_no, "
                "contribution_rank, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(dashboard_biz_no, drill_biz_no) DO UPDATE SET "
                "run_id = excluded.run_id, batch_no = excluded.batch_no, "
                "contribution_rank = excluded.contribution_rank, created_at = excluded.created_at",
                (dashboard_biz_no, drill_biz_no, run_id, batch_no, contribution_rank, utc_now()),
            )

    def list_links(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM drill_links ORDER BY dashboard_biz_no, drill_biz_no"
            ).fetchall()
            return [dict(row) for row in rows]

    # ---------------- 审计 ----------------
    def add_audit(
        self,
        *,
        action: str,
        run_id: int | None = None,
        module: str | None = None,
        biz_no: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO mig_audit(run_id, module, biz_no, action, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, module, biz_no, action, detail, utc_now()),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mig_audit ORDER BY audit_id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ---------------- 持久化看板归因明细数据集 ----------------
    def dataset_rows(self, run_id: int | None = None) -> list[dict[str, Any]]:
        """看板视图与归因分析按同一持久化批次拼出的明细数据集。"""
        sql = (
            "SELECT l.dashboard_biz_no, l.drill_biz_no, l.run_id, l.batch_no, "
            "       l.contribution_rank, "
            "       d.payload_json AS dashboard_payload, d.content_hash AS dashboard_hash, "
            "       d.version AS dashboard_version, d.source_updated_at AS dashboard_updated_at, "
            "       m.payload_json AS drill_payload, m.content_hash AS drill_hash, "
            "       m.version AS drill_version, m.source_updated_at AS drill_updated_at "
            "FROM drill_links l "
            "JOIN biz_records d ON d.module = 'dashboard' AND d.biz_no = l.dashboard_biz_no "
            "     AND d.is_current = 1 "
            "JOIN biz_records m ON m.module = 'metricdrill' AND m.biz_no = l.drill_biz_no "
            "     AND m.is_current = 1 "
        )
        params: list[Any] = []
        if run_id is not None:
            sql += " WHERE l.run_id = ?"
            params.append(run_id)
        sql += " ORDER BY l.batch_no, l.dashboard_biz_no, l.drill_biz_no"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["dashboard_payload"] = json.loads(item["dashboard_payload"])
            item["drill_payload"] = json.loads(item["drill_payload"])
            result.append(item)
        return result

    # ---------------- 辅助 ----------------
    @staticmethod
    def _hydrate(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["is_current"] = bool(item["is_current"])
        return item


persistent_store = PersistentStore()

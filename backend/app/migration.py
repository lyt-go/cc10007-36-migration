"""首批内存看板视图与指标归因记录的分批迁移引擎。

职责：
- 分批读取内存 Store 中 dashboard / metricdrill 两个模块的首批记录并写入 SQLite；
- 迁移运行期间冻结这两个模块的一切业务写入，拦截动作写入审计表；
- 同一业务编号出现多个可迁移终态时，按「更新时间更晚者为当前版本」裁决，
  其余版本保留为可回查的历史版本，每个编号始终只有一个当前版本；
- 切换后看板视图与归因分析都从同一持久化批次读取，贡献度排名绑定看板编号；
- 重复执行按内容哈希幂等跳过，重启后按 mig_cursor 断点继续。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from app.config import settings
from app.persistence import (
    MIGRATING_MODULES,
    RUN_DONE,
    RUN_IDLE,
    RUN_MIGRATING,
    PersistentStore,
    persistent_store,
)
from app.store import store

# 各模块的业务编号字段（业务编号在切换后必须保持完整）
BIZ_FIELD = {
    "dashboard": "视图编号",
    "metricdrill": "分析编号",
}

# 看板编号与归因分析编号按同一序号配对（DASH-0002 ↔ METR-0002），
# 贡献度排名随关联关系绑定到对应看板编号。
BIZ_SUFFIX_PREFIX = {
    "dashboard": "DASH-",
    "metricdrill": "METR-",
}


def canonical_hash(payload: dict[str, Any]) -> str:
    """对业务行做规范化哈希：同样的内容在重复执行时得到同一指纹，用于幂等判定。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def source_updated_at(row: dict[str, Any]) -> str:
    """提取内存行的更新时间；样例数据没有时间戳时按主键生成确定性兜底时间。"""
    for key in ("更新时间", "updated_at", "更新于"):
        raw = row.get(key)
        if raw:
            text = str(raw).strip()
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
            except ValueError:
                try:
                    return datetime.strptime(text, "%Y-%m-%d").isoformat()
                except ValueError:
                    return text
    row_id = int(row.get("id", 0))
    # 兜底：同一行每次迁移得到相同时间，保证可重复核对；id 越大时间越晚。
    return f"2026-01-01T00:{row_id:02d}:00+00:00"


class MigrationEngine:
    def __init__(self, db: PersistentStore | None = None) -> None:
        self.db = db or persistent_store
        self._lock = threading.RLock()

    # ---------------- 状态与冻结 ----------------
    def current_state(self) -> str:
        run = self.db.get_latest_run()
        if run is None:
            return RUN_IDLE
        return str(run["status"])

    def is_frozen(self) -> bool:
        """迁移进行中写入冻结；切换完成后写入改走持久库，不再经过内存。"""
        return self.current_state() == RUN_MIGRATING

    def is_cutover(self) -> bool:
        return self.current_state() == RUN_DONE

    def record_blocked_write(
        self, module: str, biz_no: str | None, detail: str
    ) -> None:
        run = self.db.get_latest_run()
        run_id = int(run["run_id"]) if run else None
        self.db.add_audit(
            run_id=run_id, module=module, biz_no=biz_no,
            action="写入冻结拦截", detail=detail,
        )

    # ---------------- 迁移主流程 ----------------
    def start_migration(
        self,
        *,
        batch_size: int | None = None,
        max_batches: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """启动或继续一次分批迁移；返回迁移运行状态与进度。"""
        with self._lock:
            size = batch_size or settings.migration_batch_size
            cap = settings.migration_max_batches if max_batches is None else max_batches
            if cap == 0:
                cap = None  # 0 表示不限制批次数，一次迁移到完成
            latest = self.db.get_latest_run()

            if latest is not None and latest["status"] == RUN_MIGRATING:
                run_id = int(latest["run_id"])
            elif latest is not None and latest["status"] == RUN_DONE:
                # 重复执行：沿用新一轮运行，内容哈希相同的记录会被幂等跳过。
                run_id = self.db.insert_run(
                    batch_size=size,
                    total_planned=sum(len(store.rows(m)) for m in MIGRATING_MODULES),
                    note=note or "重复执行迁移（幂等核对）",
                )
                self._init_cursors(run_id)
            else:
                run_id = self.db.insert_run(
                    batch_size=size,
                    total_planned=sum(len(store.rows(m)) for m in MIGRATING_MODULES),
                    note=note,
                )
                self._init_cursors(run_id)

            self.db.add_audit(run_id=run_id, action="迁移启动", detail=f"batch_size={size}")
            processed = self._run_batches(run_id, size, cap)
            return self.status(run_id=run_id, extra={"processed_this_call": processed})

    def resume_on_startup(self) -> dict[str, Any] | None:
        """服务启动时若发现上次迁移中断（进程重启），从断点继续到完成。"""
        with self._lock:
            latest = self.db.get_latest_run()
            if latest is None or latest["status"] != RUN_MIGRATING:
                return None
            run_id = int(latest["run_id"])
            # 断点已在库里，内存游标以库为准重新对齐。
            processed = self._run_batches(run_id, int(latest["batch_size"]), None)
            self.db.add_audit(
                run_id=run_id, action="重启后续迁",
                detail=f"从断点继续，本次补迁 {processed} 个批次",
            )
            return self.status(run_id=run_id, extra={"processed_this_call": processed})

    def _init_cursors(self, run_id: int) -> None:
        for module in MIGRATING_MODULES:
            self.db.upsert_cursor(module, run_id, 0)

    def _run_batches(self, run_id: int, batch_size: int, max_batches: int | None) -> int:
        batches_done = 0
        while True:
            if max_batches is not None and batches_done >= max_batches:
                break
            progressed = False
            for module in MIGRATING_MODULES:
                cursor = self.db.get_cursor(module)
                offset = int(cursor["next_offset"]) if cursor else 0
                source_rows = store.rows(module)
                if offset >= len(source_rows):
                    continue
                chunk = source_rows[offset:offset + batch_size]
                self._migrate_batch(run_id, module, offset, chunk)
                self.db.upsert_cursor(module, run_id, offset + len(chunk))
                batches_done += 1
                progressed = True
                if max_batches is not None and batches_done >= max_batches:
                    break
            if not progressed:
                break

        # 两个模块都到断点末尾才允许切换，保证「两个完整看板版本」一起落库。
        if self._all_cursors_reached_end():
            self._cutover(run_id)
        return batches_done

    def _all_cursors_reached_end(self) -> bool:
        for module in MIGRATING_MODULES:
            cursor = self.db.get_cursor(module)
            if cursor is None:
                return False
            if int(cursor["next_offset"]) < len(store.rows(module)):
                return False
        return True

    def _migrate_batch(
        self,
        run_id: int,
        module: str,
        offset: int,
        chunk: list[dict[str, Any]],
    ) -> None:
        batch_no = self._next_batch_no(run_id, module)
        if self.db.batch_exists(run_id, batch_no, module):
            return
        biz_field = BIZ_FIELD[module]
        # 同批次内同业务编号可能出现多个终态（切换冲突场景），先收集再统一裁决。
        current_candidates: dict[str, tuple[str, int]] = {}
        batch_payloads: list[str] = []

        for row in chunk:
            payload = dict(row)
            biz_no = str(payload.get(biz_field) or "").strip()
            if not biz_no:
                continue
            updated_at = source_updated_at(payload)
            digest = canonical_hash(payload)
            batch_payloads.append(digest)

            existing = self.db.find_migrated_version(module, biz_no, digest)
            if existing is not None:
                # 重复执行：内容一致，幂等跳过，但仍参与同批次当前版本裁决。
                candidate = (updated_at, int(existing["record_id"]))
                if biz_no not in current_candidates or candidate > current_candidates[biz_no]:
                    current_candidates[biz_no] = candidate
                continue

            version = self.db.latest_version(module, biz_no) + 1
            record_id = self.db.insert_record(
                module=module,
                biz_no=biz_no,
                version=version,
                is_current=False,
                row_id=int(payload.get("id", 0)),
                payload=payload,
                content_hash=digest,
                run_id=run_id,
                batch_no=batch_no,
                source_updated_at=updated_at,
            )
            candidate = (updated_at, record_id)
            if biz_no not in current_candidates or candidate > current_candidates[biz_no]:
                current_candidates[biz_no] = candidate

        # 裁决当前版本：更新时间更晚者成为唯一当前版本，其余保留为历史版本。
        for biz_no, (_updated_at, record_id) in current_candidates.items():
            self.db.clear_current(module, biz_no)
            record = self.db.get_record(record_id)
            if record is not None:
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE biz_records SET is_current = 1 WHERE record_id = ?",
                        (record_id,),
                    )

        content_hash = hashlib.sha256("|".join(batch_payloads).encode("utf-8")).hexdigest()
        self.db.insert_batch(
            run_id=run_id,
            batch_no=batch_no,
            module=module,
            offset_start=offset,
            offset_end=offset + len(chunk),
            row_count=len(chunk),
            content_hash=content_hash,
        )

    def _next_batch_no(self, run_id: int, module: str) -> int:
        batches = [b for b in self.db.list_batches(run_id) if b["module"] == module]
        return len(batches) + 1

    def _cutover(self, run_id: int) -> None:
        """全部批次落库后切换：重建看板↔归因关联，并把运行标记为完成。"""
        self._rebuild_links(run_id)
        migrated = sum(
            1
            for module in MIGRATING_MODULES
            for _ in range(self.db.count_current(module))
        )
        self.db.update_run(
            run_id, status=RUN_DONE, total_migrated=migrated,
            finished_at=self._utc_now(), note=None,
        )
        self.db.add_audit(run_id=run_id, action="迁移切换完成",
                          detail=f"持久化当前版本共 {migrated} 条")

    def _rebuild_links(self, run_id: int) -> None:
        """贡献度排名绑定对应看板编号；两边取同一持久化运行里的当前版本。"""
        dash_rows = self.db.list_current("dashboard")
        drill_rows = self.db.list_current("metricdrill")
        dash_by_suffix = {
            self._suffix(row["biz_no"], BIZ_SUFFIX_PREFIX["dashboard"]): row
            for row in dash_rows
        }
        for drill in drill_rows:
            suffix = self._suffix(drill["biz_no"], BIZ_SUFFIX_PREFIX["metricdrill"])
            dash = dash_by_suffix.get(suffix)
            if dash is None:
                continue
            rank = str(drill["payload"].get("贡献度排名") or "")
            self.db.upsert_link(
                dashboard_biz_no=dash["biz_no"],
                drill_biz_no=drill["biz_no"],
                run_id=run_id,
                batch_no=int(drill["batch_no"] or 0),
                contribution_rank=rank,
            )

    @staticmethod
    def _suffix(biz_no: str, prefix: str) -> str:
        return biz_no.split(prefix, 1)[1] if prefix in biz_no else biz_no

    # ---------------- 核对视图 ----------------
    def status(self, *, run_id: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        run = self.db.get_run(run_id) if run_id else self.db.get_latest_run()
        result: dict[str, Any] = {
            "state": self.current_state(),
            "frozen": self.is_frozen(),
            "cutover": self.is_cutover(),
            "run": run,
            "cursors": self.db.all_cursors(),
            "modules": {
                module: {
                    "memory_rows": len(store.rows(module)),
                    "persistent_current": self.db.count_current(module),
                    "biz_field": BIZ_FIELD[module],
                }
                for module in MIGRATING_MODULES
            },
        }
        if extra:
            result.update(extra)
        return result

    def dataset(self, run_id: int | None = None) -> dict[str, Any]:
        rows = self.db.dataset_rows(run_id)
        return {
            "run_id": run_id,
            "total": len(rows),
            "items": rows,
        }

    @staticmethod
    def _utc_now() -> str:
        from app.persistence import utc_now

        return utc_now()


engine = MigrationEngine()

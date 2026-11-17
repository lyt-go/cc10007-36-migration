"""切换后的持久化读写网关。

dashboard / metricdrill 两个服务在迁移切换后通过本网关读写 SQLite，
接口形状与原内存服务保持一致（列表分页、单条读取、登记、动作流转），
业务编号与关联关系不改变；每次状态流转都落一个新版本，历史版本仍可回查。
"""
from __future__ import annotations

from typing import Any

from app.migration import engine, source_updated_at
from app.persistence import persistent_store
from app.store import store


class PersistentGateway:
    def __init__(
        self,
        module: str,
        biz_field: str,
        required_fields: list[str],
        status_order: list[str],
    ) -> None:
        self.module = module
        self.biz_field = biz_field
        self.required_fields = required_fields
        self.status_order = status_order

    # ---------------- 读 ----------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._to_view(record) for record in persistent_store.list_current(self.module)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get(self.biz_field, ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        for record in persistent_store.list_current(self.module):
            if int(record.get("row_id") or 0) == entry_id:
                return self._to_view(record)
        return None

    # ---------------- 写（仅切换后调用；冻结期由服务层拦截） ----------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in self.required_fields if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        new_id = max(persistent_store.max_persistent_row_id(self.module),
                     max((int(row.get("id", 0)) for row in store.rows(self.module)), default=0)) + 1
        payload: dict[str, Any] = {"id": new_id}
        payload.update({field: values.get(field) for field in self.required_fields})
        payload["status"] = self.status_order[0]
        payload["pending"] = True
        payload["abnormal"] = False
        payload["更新时间"] = source_updated_at(payload)
        return self._persist_new_version(payload, action="切换后登记"), []

    def apply_action(
        self, entry_id: int, action: str, target_status: str, negative: bool
    ) -> tuple[dict[str, Any] | None, str]:
        current = None
        for record in persistent_store.list_current(self.module):
            if int(record.get("row_id") or 0) == entry_id:
                current = record
                break
        if current is None:
            return None, f"记录 {entry_id} 不存在或已归档"
        payload = dict(current["payload"])
        payload["status"] = target_status
        payload["pending"] = target_status != self.status_order[-1]
        payload["abnormal"] = negative
        payload["更新时间"] = source_updated_at(payload)
        view = self._persist_new_version(payload, action=f"切换后动作:{action}")
        return view, f"已{action}（持久化新版本）"

    # ---------------- 版本落库 ----------------
    def _persist_new_version(self, payload: dict[str, Any], *, action: str) -> dict[str, Any]:
        from app.migration import canonical_hash

        biz_no = str(payload.get(self.biz_field) or "").strip()
        digest = canonical_hash(payload)
        existing = persistent_store.find_migrated_version(self.module, biz_no, digest)
        if existing is None:
            version = persistent_store.latest_version(self.module, biz_no) + 1
            record_id = persistent_store.insert_record(
                module=self.module,
                biz_no=biz_no,
                version=version,
                is_current=False,
                row_id=int(payload.get("id", 0)),
                payload=payload,
                content_hash=digest,
                run_id=None,
                batch_no=None,
                source_updated_at=source_updated_at(payload),
            )
            persistent_store.clear_current(self.module, biz_no)
            with persistent_store.transaction() as conn:
                conn.execute(
                    "UPDATE biz_records SET is_current = 1 WHERE record_id = ?", (record_id,)
                )
            record = persistent_store.get_record(record_id)
        else:
            record = existing
        run = persistent_store.get_latest_run()
        persistent_store.add_audit(
            run_id=int(run["run_id"]) if run else None,
            module=self.module,
            biz_no=biz_no,
            action=action,
            detail=f"record_id={record['record_id']} version={record['version']}",
        )
        return self._to_view(record)

    def _to_view(self, record: dict[str, Any]) -> dict[str, Any]:
        view = dict(record["payload"])
        view["_persisted"] = {
            "record_id": record["record_id"],
            "version": record["version"],
            "content_hash": record["content_hash"],
            "run_id": record["run_id"],
            "batch_no": record["batch_no"],
            "source_updated_at": record["source_updated_at"],
        }
        return view


def freeze_detail(module: str, action: str) -> str:
    return f"模块 {module} 处于迁移写入冻结期，动作 {action} 被拒绝并留痕"


def guard_write(module: str, biz_no: str | None, action: str) -> bool:
    """服务层写入前调用：冻结期拦截并写审计；返回 True 表示放行。"""
    if engine.is_frozen():
        engine.record_blocked_write(module, biz_no, freeze_detail(module, action))
        return False
    return True

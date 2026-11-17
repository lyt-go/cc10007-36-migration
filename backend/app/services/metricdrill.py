"""指标归因业务规则：状态流转、字段校验与筛选口径都收在这里。

迁移期行为与运营看板一致：冻结期拦截写入并留痕，切换后读写走持久库，
贡献度排名随关联关系绑定对应看板编号。
"""
from __future__ import annotations

from typing import Any

from app.migration import engine
from app.persistent_gateway import PersistentGateway, guard_write
from app.store import store

MODULE = "metricdrill"
REQUIRED_FIELDS = ["分析编号", "目标指标", "波动方向"]
STATUS_ORDER = ["草稿", "分析中", "已确认", "已归档"]
ACTION_RULES = {"发起分析": "分析中", "确认结论": "已确认", "归档分析": "已归档"}
NEGATIVE_ACTIONS: list[str] = []

gateway = PersistentGateway(MODULE, "分析编号", REQUIRED_FIELDS, STATUS_ORDER)


class MetricdrillService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        if engine.is_cutover():
            return gateway.list_entries(keyword=keyword, status=status, page=page, size=size)
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("分析编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        if engine.is_cutover():
            return gateway.get_entry(entry_id)
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        if engine.is_cutover():
            if not guard_write(MODULE, str(values.get("分析编号") or ""), "登记归因分析"):
                return None, []
            return gateway.create_entry(values)
        if engine.is_frozen():
            guard_write(MODULE, str(values.get("分析编号") or ""), "登记归因分析")
            return None, []
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if engine.is_cutover():
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于指标归因可执行范围"
            if not guard_write(MODULE, None, action):
                return None, "迁移写入冻结期，操作已被拦截并记录审计"
            return gateway.apply_action(
                entry_id, action, ACTION_RULES[action], action in NEGATIVE_ACTIONS
            )
        if engine.is_frozen():
            guard_write(MODULE, None, action)
            return None, "迁移写入冻结期，操作已被拦截并记录审计"
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"归因分析 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标归因可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"归因分析已{action}"

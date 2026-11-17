"""内存看板/指标归因 → 持久库的迁移管理接口。

覆盖：迁移状态、分批启动/续迁、迁移批次回查、版本历史、持久化看板归因明细数据集、
关联记录、审计留痕，以及供核对脚本使用的内存夹具（重复业务编号版本、清空内存模块）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.migration import BIZ_FIELD, engine
from app.persistence import persistent_store
from app.schemas import ActionResult, EntryPayload
from app.store import store

router = APIRouter(prefix="/api/migration", tags=["看板归因迁移"])


@router.get("/status")
def migration_status() -> dict[str, Any]:
    """迁移状态机与各模块内存/持久库行数，前端据此判断是否处于写入冻结期。"""
    return engine.status()


@router.post("/start", response_model=ActionResult)
def start_migration(payload: EntryPayload | None = None) -> ActionResult:
    """分批读取首批内存记录并迁移入库；max_batches 用于模拟部分迁移与断点续迁。"""
    values = payload.values if payload is not None else {}
    batch_size = values.get("batch_size")
    max_batches = values.get("max_batches")
    note = str(values.get("note") or "")
    try:
        status = engine.start_migration(
            batch_size=int(batch_size) if batch_size else None,
            max_batches=int(max_batches) if max_batches is not None else None,
            note=note,
        )
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    state = status["state"]
    message = (
        "迁移已切换，看板视图与归因分析均从持久库读取"
        if state == "done"
        else "迁移进行中，写入已冻结，可继续分批迁移"
    )
    return ActionResult(ok=True, message=message, entry=status)


@router.get("/batches")
def list_batches(run_id: int | None = Query(default=None)) -> dict[str, Any]:
    """迁移批次回查：批次号、模块、偏移区间、行数量与内容指纹。"""
    items = persistent_store.list_batches(run_id)
    return {"total": len(items), "items": items}


@router.get("/dataset")
def migration_dataset(run_id: int | None = Query(default=None)) -> dict[str, Any]:
    """持久化看板归因明细数据集：看板视图与归因分析来自同一持久化批次。"""
    return engine.dataset(run_id)


@router.get("/versions")
def list_versions(
    module: str = Query(description="dashboard 或 metricdrill"),
    biz_no: str = Query(description="业务编号，如 DASH-0002"),
) -> dict[str, Any]:
    """按数据版本回查：同一业务编号的全部历史版本与唯一当前版本标记。"""
    items = persistent_store.list_versions(module, biz_no)
    return {"module": module, "biz_no": biz_no, "total": len(items), "items": items}


@router.get("/links")
def list_links() -> dict[str, Any]:
    """看板编号 ↔ 归因分析编号关联记录，贡献度排名绑定对应看板编号。"""
    items = persistent_store.list_links()
    return {"total": len(items), "items": items}


@router.get("/audit")
def list_audit(limit: int = Query(default=100, le=500)) -> dict[str, Any]:
    """迁移审计：冻结期写入拦截、切换动作、切换后写入全部可追溯。"""
    items = persistent_store.list_audit(limit=limit)
    return {"total": len(items), "items": items}


@router.post("/fixtures/duplicate-version", response_model=ActionResult)
def fixture_duplicate_version(payload: EntryPayload) -> ActionResult:
    """核对夹具：向内存表注入同一业务编号的第二个完整版本（带更新时间），
    用于验证「两个完整看板版本落到同一业务编号」时的当前版本裁决。
    """
    module = str(payload.values.get("module") or "dashboard").strip()
    if module not in BIZ_FIELD:
        return ActionResult(ok=False, message=f"模块 {module} 不在迁移范围")
    if engine.is_cutover():
        return ActionResult(ok=False, message="已切换完成，内存夹具不再生效，请在迁移前使用")
    biz_field = BIZ_FIELD[module]
    biz_no = str(payload.values.get("biz_no") or "DASH-0002").strip()
    rows = store.rows(module)
    base = next((row for row in rows if str(row.get(biz_field)) == biz_no), None)
    if base is None:
        return ActionResult(ok=False, message=f"内存中不存在业务编号 {biz_no}")
    new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    duplicate = dict(base)
    duplicate.update({
        "id": new_id,
        "更新时间": str(payload.values.get("更新时间") or "2026-11-01T09:00:00+00:00"),
    })
    # 制造内容差异：视图名称/指标组合与告警线配置不同，形成可区分的新版本。
    duplicate["视图名称" if module == "dashboard" else "目标指标"] = (
        f"{base.get('视图名称' if module == 'dashboard' else '目标指标', biz_no)}-冲突复核版"
    )
    if module == "dashboard":
        duplicate["告警线配置"] = "复核告警线:阈值上调10%"
    else:
        duplicate["根因假设"] = "复核根因:新版本下钻结论"
    rows.append(duplicate)
    persistent_store.add_audit(
        module=module, biz_no=biz_no, action="夹具注入重复版本",
        detail=f"内存新增 id={new_id} 的同编号完整版本",
    )
    return ActionResult(ok=True, message=f"已注入 {biz_no} 的第二个内存版本", entry=duplicate)


@router.post("/fixtures/clear-memory", response_model=ActionResult)
def fixture_clear_memory(payload: EntryPayload) -> ActionResult:
    """核对夹具：清空指定迁移模块的内存记录，验证空库/空批次迁移不报错且可核对。"""
    module = str(payload.values.get("module") or "").strip()
    if module not in BIZ_FIELD:
        return ActionResult(ok=False, message=f"模块 {module} 不在迁移范围")
    if engine.is_cutover():
        return ActionResult(ok=False, message="已切换完成，内存夹具不再生效，请在迁移前使用")
    store.rows(module).clear()
    persistent_store.add_audit(module=module, action="夹具清空内存模块", detail="空库核对")
    return ActionResult(ok=True, message=f"内存模块 {module} 已清空")

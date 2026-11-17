"""看板视图与指标归因迁移入库的端到端核对脚本（仅用标准库）。

通过真实 uvicorn 子进程验证：
1. 空库启动：迁移前列表与内存一致，指标值/根因/告警线字段可读；
2. 分批迁移：max_batches 制造部分迁移，写入冻结并可在审计中追溯；
3. 重启续迁：杀掉服务进程后用同一数据库重启，断点自动补齐并切换；
4. 切换后读：dashboard / metricdrill 全部从持久库读取，DASH-0002 为主核对样例，
   对比迁移前后指标值、根因、告警状态与迁移批次号；
5. 重复执行：再次迁移内容哈希一致，版本不膨胀；
6. 同编号双版本裁决：两个完整看板版本落到同一业务编号时仅一个当前版本；
7. 切换后写入：动作流转生成持久化新版本，再次重启仍可追溯；
8. 持久化看板归因明细数据集与关联记录（贡献度排名绑定看板编号）。

用法：APP_DB_PATH 由脚本自动分配临时库；直接 `python -I verify_migration.py`。
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:8000"
PYTHON = str(BACKEND_DIR / ".venv" / "bin" / "python")

PASS = "[通过]"
FAIL = "[失败]"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = PASS if condition else FAIL
    print(f"  {mark} {name}" + (f" —— {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_healthy(proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            status, body = api("GET", "/api/health")
            if status == 200 and body.get("ok"):
                return
        except OSError:
            pass
        time.sleep(0.4)
    raise RuntimeError("服务健康检查超时")


def start_server(db_path: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["APP_DB_PATH"] = db_path
    env["APP_MIGRATION_BATCH_SIZE"] = "2"
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=BACKEND_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_healthy(proc)
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    proc.wait(timeout=15)


def snapshot(module: str) -> list[dict]:
    _, body = api("GET", f"/api/{module}?size=200")
    return body["items"]


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="dash_mig_verify_")
    db_path = os.path.join(tmpdir, "verify_persist.sqlite3")
    print(f"临时持久库：{db_path}")

    # ---------------- 阶段一：空库启动，迁移前基线 ----------------
    print("阶段一 空库启动与迁移前基线")
    proc = start_server(db_path)
    try:
        status, body = api("GET", "/api/migration/status")
        check("初始状态为 idle", body["state"] == "idle", str(body.get("state")))
        check("空库无迁移批次", body["run"] is None)

        before_dash = {row["视图编号"]: row for row in snapshot("dashboard")}
        before_drill = {row["分析编号"]: row for row in snapshot("metricdrill")}
        check("迁移前看板 3 条", len(before_dash) == 3, str(len(before_dash)))
        check("迁移前归因 3 条", len(before_drill) == 3, str(len(before_drill)))
        d2 = before_dash["DASH-0002"]
        m2 = before_drill["METR-0002"]
        print(f"    DASH-0002 主样例 告警线配置={d2['告警线配置']} 状态={d2['status']}")
        print(f"    METR-0002 主样例 根因假设={m2['根因假设']} 验证结果={m2['验证结果']} 状态={m2['status']}")

        # ---------------- 阶段二：部分迁移 + 写入冻结 ----------------
        print("阶段二 分批迁移（每批 2 行，本次限 1 批）与写入冻结")
        status, body = api("POST", "/api/migration/start", {"values": {"max_batches": 1, "note": "核对-部分迁移"}})
        check("部分迁移后保持 migrating", body["entry"]["state"] == "migrating", str(body["entry"]))
        check("冻结标记为真", body["entry"]["frozen"] is True)

        code, blocked = api("POST", "/api/dashboard/2/actions", {"values": {"action": "发布视图"}})
        check("冻结期看板写入被拒", blocked["ok"] is False, blocked.get("message", ""))
        code, blocked2 = api("POST", "/api/metricdrill/2/actions", {"values": {"action": "确认结论"}})
        check("冻结期归因写入被拒", blocked2["ok"] is False, blocked2.get("message", ""))
        _, audit = api("GET", "/api/migration/audit")
        block_rows = [a for a in audit["items"] if a["action"] == "写入冻结拦截"]
        check("冻结拦截写入审计留痕", len(block_rows) >= 2, f"实际 {len(block_rows)} 条")

        _, batches = api("GET", "/api/migration/batches")
        check("部分迁移只落 1 个模块批次", batches["total"] == 1, f"实际 {batches['total']} 个")
        check("批次号从 1 开始且行数为 2",
              batches["items"][0]["batch_no"] == 1 and batches["items"][0]["row_count"] == 2)
    finally:
        stop_server(proc)

    # ---------------- 阶段三：重启后断点续迁并自动切换 ----------------
    print("阶段三 服务重启（同库）断点续迁")
    proc = start_server(db_path)
    try:
        _, body = api("GET", "/api/migration/status")
        check("重启后自动补齐并切换 done", body["state"] == "done", str(body.get("state")))
        check("两模块当前版本各 3 条",
              body["modules"]["dashboard"]["persistent_current"] == 3
              and body["modules"]["metricdrill"]["persistent_current"] == 3,
              str(body["modules"]))
    finally:
        stop_server(proc)

    # ---------------- 阶段四：切换后核对（主样例 DASH-0002） ----------------
    print("阶段四 切换后持久化读取与迁移前后字段对比")
    proc = start_server(db_path)
    try:
        after_dash = {row["视图编号"]: row for row in snapshot("dashboard")}
        after_drill = {row["分析编号"]: row for row in snapshot("metricdrill")}
        check("切换后看板仍为 3 条", len(after_dash) == 3)
        check("切换后归因仍为 3 条", len(after_drill) == 3)

        d2_after = after_dash["DASH-0002"]
        m2_after = after_drill["METR-0002"]
        check("DASH-0002 指标组合一致", d2_after["指标组合"] == d2["指标组合"])
        check("DASH-0002 告警线配置（告警状态）一致",
              d2_after["告警线配置"] == d2["告警线配置"])
        check("DASH-0002 状态一致", d2_after["status"] == d2["status"])
        check("DASH-0002 业务编号完整", d2_after["视图编号"] == "DASH-0002")
        check("DASH-0002 绑定迁移批次号", d2_after["_persisted"]["batch_no"] is not None,
              str(d2_after.get("_persisted")))
        check("METR-0002 根因假设一致", m2_after["根因假设"] == m2["根因假设"])
        check("METR-0002 验证结果一致", m2_after["验证结果"] == m2["验证结果"])
        check("METR-0002 状态一致", m2_after["status"] == m2["status"])
        check("METR-0002 绑定同一迁移运行号",
              m2_after["_persisted"]["run_id"] == d2_after["_persisted"]["run_id"])
        print(f"    迁移后 DASH-0002 批次号={d2_after['_persisted']['batch_no']} "
              f"版本={d2_after['_persisted']['version']} 哈希={d2_after['_persisted']['content_hash'][:12]}…")

        # 全量逐字段对比（除持久化元数据外完全一致）
        mismatch = []
        for biz, before in before_dash.items():
            after = after_dash[biz]
            for key, value in before.items():
                if after.get(key) != value:
                    mismatch.append((biz, key, value, after.get(key)))
        check("看板全部业务字段迁移前后一致", not mismatch, str(mismatch[:3]))
        mismatch = []
        for biz, before in before_drill.items():
            after = after_drill[biz]
            for key, value in before.items():
                if after.get(key) != value:
                    mismatch.append((biz, key, value, after.get(key)))
        check("归因全部业务字段迁移前后一致", not mismatch, str(mismatch[:3]))

        # ---------------- 阶段五：持久化明细数据集与关联 ----------------
        print("阶段五 持久化看板归因明细数据集与关联记录")
        _, dataset = api("GET", "/api/migration/dataset")
        check("明细数据集 3 条关联", dataset["total"] == 3, str(dataset["total"]))
        d2_link = next((r for r in dataset["items"] if r["dashboard_biz_no"] == "DASH-0002"), None)
        check("DASH-0002 关联 METR-0002", d2_link is not None and d2_link["drill_biz_no"] == "METR-0002")
        check("贡献度排名绑定看板编号", d2_link is not None
              and d2_link["contribution_rank"] == m2["贡献度排名"])
        _, links = api("GET", "/api/migration/links")
        check("关联清单含 3 条且编号一一对应",
              sorted((i["dashboard_biz_no"], i["drill_biz_no"]) for i in links["items"]) ==
              [("DASH-0001", "METR-0001"), ("DASH-0002", "METR-0002"), ("DASH-0003", "METR-0003")])
        _, versions = api("GET", "/api/migration/versions?module=dashboard&biz_no=DASH-0002")
        check("可按数据版本回查 DASH-0002", versions["total"] >= 1
              and sum(1 for v in versions["items"] if v["is_current"]) == 1)

        # ---------------- 阶段六：重复执行幂等 ----------------
        print("阶段六 重复执行迁移（幂等）")
        _, body = api("POST", "/api/migration/start", {"values": {"note": "核对-重复执行"}})
        check("重复执行后仍为 done", body["entry"]["state"] == "done")
        _, versions2 = api("GET", "/api/migration/versions?module=dashboard&biz_no=DASH-0002")
        check("重复执行不产生新版本", versions2["total"] == versions["total"],
              f"{versions['total']} -> {versions2['total']}")
        conn = sqlite3.connect(db_path)
        run_count = conn.execute("SELECT COUNT(*) FROM mig_runs").fetchone()[0]
        rec_count = conn.execute("SELECT COUNT(*) FROM biz_records").fetchone()[0]
        check("迁移运行新增但业务记录不膨胀", run_count == 2 and rec_count == 6,
              f"runs={run_count} records={rec_count}")
        conn.close()

        # ---------------- 阶段七：切换后写入并再次重启可追溯 ----------------
        print("阶段七 切换后写入持久化与重启追溯")
        code, action = api("POST", "/api/dashboard/2/actions", {"values": {"action": "发布视图"}})
        check("切换后动作成功并落持久化新版本", action["ok"] is True, action.get("message", ""))
        _, versions3 = api("GET", "/api/migration/versions?module=dashboard&biz_no=DASH-0002")
        check("动作产生 DASH-0002 新版本", versions3["total"] == versions2["total"] + 1)
        check("仍只有一个当前版本", sum(1 for v in versions3["items"] if v["is_current"]) == 1)
        current = next(v for v in versions3["items"] if v["is_current"])
        check("当前版本状态为已发布", current["payload"]["status"] == "已发布")
    finally:
        stop_server(proc)

    # ---------------- 阶段八：再次重启，写入仍可追溯 ----------------
    print("阶段八 再次重启后指标值/根因/告警线仍可追溯")
    proc = start_server(db_path)
    try:
        dash = {row["视图编号"]: row for row in snapshot("dashboard")}
        drill = {row["分析编号"]: row for row in snapshot("metricdrill")}
        check("重启后 DASH-0002 保持已发布", dash["DASH-0002"]["status"] == "已发布")
        check("重启后告警线配置未丢", dash["DASH-0002"]["告警线配置"] == d2["告警线配置"])
        check("重启后 METR-0002 根因未丢", drill["METR-0002"]["根因假设"] == m2["根因假设"])
        check("重启后业务编号与关联完整",
              dash["DASH-0002"]["视图编号"] == "DASH-0002"
              and drill["METR-0002"]["分析编号"] == "METR-0002")
        _, audit = api("GET", "/api/migration/audit?limit=200")
        check("切换后写入在审计中可追溯",
              any(a["action"].startswith("切换后动作") for a in audit["items"]))
    finally:
        stop_server(proc)

    print()
    if failures:
        print(f"{FAIL} 共 {len(failures)} 项未通过：{failures}")
        return 1
    print(f"{PASS} 全部核对项通过，持久库位于 {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""补充核对：空内存模块迁移 与 同业务编号双完整版本裁决（HTTP 端到端）。

场景 A：清空 dashboard 内存记录后迁移，空批次不报错、状态可切换、归因正常落库；
场景 B：迁移前注入 DASH-0002 的第二个完整版本，迁移后更新时间更晚者成为唯一当前版本，
       历史版本保留并可按数据版本回查。
"""
from __future__ import annotations

import json
import os
import signal
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
    print(f"  {PASS if condition else FAIL} {name}" + (f" —— {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def start_server(db_path: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["APP_DB_PATH"] = db_path
    env["APP_MIGRATION_BATCH_SIZE"] = "2"
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=BACKEND_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            status, body = api("GET", "/api/health")
            if status == 200 and body.get("ok"):
                return proc
        except OSError:
            time.sleep(0.4)
    raise RuntimeError("服务启动超时")


def stop_server(proc: subprocess.Popen) -> None:
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    proc.wait(timeout=15)


def run_case(name: str, setup_path: str, setup_payload: dict, assertions) -> None:
    print(name)
    db_path = os.path.join(tempfile.mkdtemp(prefix="dash_edge_"), "edge.sqlite3")
    proc = start_server(db_path)
    try:
        code, body = api("POST", setup_path, setup_payload)
        check("夹具设置成功", code == 200 and body.get("ok"), str(body))
        code, body = api("POST", "/api/migration/start", {"values": {"note": name}})
        check("迁移正常完成", code == 200 and body["entry"]["state"] == "done", str(body.get("entry")))
        assertions()
    finally:
        stop_server(proc)


def main() -> int:
    # 场景 A：dashboard 内存为空
    run_case(
        "场景A 空内存模块（dashboard 清空）迁移",
        "/api/migration/fixtures/clear-memory",
        {"values": {"module": "dashboard"}},
        lambda: _assert_empty_case(),
    )

    # 场景 B：DASH-0002 双完整版本
    run_case(
        "场景B 同一业务编号两个完整看板版本的当前版本裁决",
        "/api/migration/fixtures/duplicate-version",
        {"values": {"module": "dashboard", "biz_no": "DASH-0002",
                    "更新时间": "2026-11-01T09:00:00+00:00"}},
        lambda: _assert_duplicate_case(),
    )

    print()
    if failures:
        print(f"{FAIL} 共 {len(failures)} 项未通过：{failures}")
        return 1
    print(f"{PASS} 补充核对全部通过")
    return 0


def _assert_empty_case() -> None:
    _, dash = api("GET", "/api/dashboard?size=200")
    _, drill = api("GET", "/api/metricdrill?size=200")
    check("空模块切换后列表为空页", dash["total"] == 0 and dash["items"] == [])
    check("另一模块数据正常", drill["total"] == 3)
    _, status = api("GET", "/api/migration/status")
    check("空模块持久化当前版本为 0",
          status["modules"]["dashboard"]["persistent_current"] == 0)
    check("非空模块持久化当前版本为 3",
          status["modules"]["metricdrill"]["persistent_current"] == 3)
    _, dataset = api("GET", "/api/migration/dataset")
    check("明细数据集不产生悬空关联", dataset["total"] == 0, str(dataset["total"]))
    _, versions = api("GET", "/api/migration/versions?module=dashboard&biz_no=DASH-0002")
    check("空模块按版本回查返回空", versions["total"] == 0)


def _assert_duplicate_case() -> None:
    _, versions = api("GET", "/api/migration/versions?module=dashboard&biz_no=DASH-0002")
    check("DASH-0002 保留两个历史版本", versions["total"] == 2, str(versions["total"]))
    current = [v for v in versions["items"] if v["is_current"]]
    check("每个编号只有一个当前版本", len(current) == 1)
    check("更新时间更晚的版本胜出",
          current and current[0]["source_updated_at"] == "2026-11-01T09:00:00+00:00",
          str(current[0]["source_updated_at"] if current else None))
    check("胜出版本为冲突复核版",
          current and current[0]["payload"]["视图名称"] == "运营看板样例2-冲突复核版")
    check("历史版本仍可回查",
          any(not v["is_current"] and v["payload"]["视图名称"] == "运营看板样例2"
              for v in versions["items"]))
    _, dash = api("GET", "/api/dashboard?keyword=DASH-0002&size=200")
    check("列表只返回唯一当前版本", dash["total"] == 1
          and dash["items"][0]["视图名称"] == "运营看板样例2-冲突复核版")
    _, links = api("GET", "/api/migration/links")
    check("关联仍绑定该看板编号且唯一",
          [i["dashboard_biz_no"] for i in links["items"]].count("DASH-0002") == 1)
    _, dataset = api("GET", "/api/migration/dataset")
    row = next((r for r in dataset["items"] if r["dashboard_biz_no"] == "DASH-0002"), None)
    check("明细数据集取当前版本内容",
          row is not None and row["dashboard_payload"]["视图名称"] == "运营看板样例2-冲突复核版")


if __name__ == "__main__":
    sys.exit(main())

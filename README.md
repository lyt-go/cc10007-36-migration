# 智能运营决策分析平台

面向用户行为序列分析、RFM分层、流失预警、智能推荐、归因建模、活动效果评估与实时运营决策的数据分析工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 行为序列 | `sequence` | 序列模式 | 模式编号、用户分群、事件序列 |
| RFM分层 | `rfm` | 分层规则 | 规则编号、最近消费天界、消费频次界 |
| 流失预警 | `churn` | 预警模型 | 模型编号、模型名称、特征变量 |
| 智能推荐 | `recommend` | 推荐策略 | 策略编号、推荐场景、召回算法 |
| 归因建模 | `attrmodel` | 归因模型 | 模型编号、模型名称、归因逻辑 |
| 活动评估 | `activity` | 活动方案 | 方案编号、活动名称、活动类型 |
| 实时决策 | `realtime` | 决策规则 | 规则编号、触发事件、决策条件 |
| 预算分配 | `budget` | 预算方案 | 方案编号、预算总额、渠道分配比例 |
| LTV估算 | `lifetime` | LTV模型 | 模型编号、模型名称、估算方法 |
| 实验变体 | `abvariant` | 变体方案 | 变体编号、关联实验、变体名称 |
| 运营看板 | `dashboard` | 看板视图 | 视图编号、视图名称、指标组合 |
| 评分卡 | `scorecard` | 评分卡 | 评分编号、评分对象、特征变量组 |
| 聚类分群 | `cluster` | 聚类任务 | 任务编号、聚类算法、特征维度 |
| 对比分析 | `comparison` | 对比组 | 对比编号、对比名称、实验组筛选 |
| 数据漂移 | `datadrift` | 漂移检测 | 检测编号、监控数据集、基线版本 |
| 推送计划 | `pushplan` | 推送方案 | 方案编号、推送内容、目标人群 |
| 指标归因 | `metricdrill` | 归因分析 | 分析编号、目标指标、波动方向 |
| 策略库 | `tacticlib` | 运营策略 | 策略编号、策略名称、策略类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 看板视图与指标归因持久化迁移

运营看板（dashboard）与指标归因（metricdrill）原本只存在进程内内存表，重启即丢。
现提供分批迁移到 SQLite 的能力，仅依赖标准库，数据库默认位于
`backend/data/ops_persist.sqlite3`（可用 `APP_DB_PATH` 覆盖）。

### 组件

| 文件 | 职责 |
| --- | --- |
| `backend/app/persistence.py` | SQLite 持久库：版本化业务记录、迁移批次、断点、看板↔归因关联、审计 |
| `backend/app/migration.py` | 分批迁移引擎：写入冻结、断点续迁、同编号双版本裁决、幂等 |
| `backend/app/persistent_gateway.py` | 切换后两个模块的持久化读写网关（与内存服务同形接口） |
| `backend/app/routers/migration.py` | 迁移管理接口与核对夹具 |
| `backend/migrate_cli.py` | 命令行分批迁移入口 |
| `backend/verify_migration.py` | 端到端核对（空库/部分迁移/重启续迁/重复执行/DASH-0002 对比） |
| `backend/verify_migration_edge.py` | 空内存模块与同编号双完整版本裁决核对 |
| `frontend/src/views/migration/index.tsx` | 看板归因迁移控制台页面 |

### 迁移语义

- 分批读取首批内存记录入库，批次号、偏移区间与内容指纹落 `mig_batches`，
  断点落 `mig_cursor`；服务重启后从断点自动继续（`app.main` 的 startup 钩子）。
- 迁移运行期间两个模块写入冻结，拦截动作写入 `mig_audit`；切换完成后写入改走持久库，
  每次状态流转新增一个版本，历史版本可按数据版本回查。
- 同一业务编号出现两个可迁移终态时，更新时间更晚者成为唯一当前版本，
  另一版本保留为历史版本（部分唯一索引保证每编号至多一个 `is_current`）。
- 看板视图与归因分析从同一持久化批次读取，按编号后缀配对（DASH-0002 ↔ METR-0002），
  贡献度排名绑定对应看板编号，明细数据集见 `GET /api/migration/dataset`。
- 重复执行按内容哈希（SHA-256）幂等跳过，业务记录不膨胀。

### 操作方式

```bash
# 接口方式（也可在前端「看板归因迁移」页面操作）
curl -X POST http://127.0.0.1:8000/api/migration/start \
  -H 'Content-Type: application/json' -d '{"values": {"max_batches": 1}}'  # 单批推进
curl -X POST http://127.0.0.1:8000/api/migration/start -d '{}'            # 迁移到完成

# 命令行方式
cd backend
.venv/bin/python migrate_cli.py --max-batches 1
.venv/bin/python migrate_cli.py

# 核对（会自动起停真实 uvicorn 子进程，使用临时库）
.venv/bin/python -I verify_migration.py
.venv/bin/python -I verify_migration_edge.py
```

核对覆盖：迁移前后指标值/根因/告警状态/迁移批次号对比、空库、部分迁移、
重复执行、重启后继续迁移与重启后写入追溯，主核对样例为 DASH-0002。

领域：数据工程
子方向：数据分析与数据应用

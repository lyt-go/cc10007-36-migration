"""业务模块路由汇总。

这里统一按别名导入再暴露 ROUTERS：模块名有可能和内置名撞车（某个业务模块就叫 dict、list
这种名字时），按名字直接 import 会把内置类型覆盖掉，函数注解在运行时求值就会报
'module' object is not subscriptable。
"""
from __future__ import annotations

from app.routers import sequence as router_sequence
from app.routers import rfm as router_rfm
from app.routers import churn as router_churn
from app.routers import recommend as router_recommend
from app.routers import attrmodel as router_attrmodel
from app.routers import activity as router_activity
from app.routers import realtime as router_realtime
from app.routers import budget as router_budget
from app.routers import lifetime as router_lifetime
from app.routers import abvariant as router_abvariant
from app.routers import dashboard as router_dashboard
from app.routers import scorecard as router_scorecard
from app.routers import cluster as router_cluster
from app.routers import comparison as router_comparison
from app.routers import datadrift as router_datadrift
from app.routers import pushplan as router_pushplan
from app.routers import metricdrill as router_metricdrill
from app.routers import tacticlib as router_tacticlib
from app.routers import migration as router_migration

ROUTERS = [router_sequence, router_rfm, router_churn, router_recommend, router_attrmodel, router_activity, router_realtime, router_budget, router_lifetime, router_abvariant, router_dashboard, router_scorecard, router_cluster, router_comparison, router_datadrift, router_pushplan, router_metricdrill, router_tacticlib, router_migration]

import { useState } from 'react'
import { Link, Route, Routes } from 'react-router-dom'

import Overview from './views/Overview'
import SequenceView from './views/sequence'
import RfmView from './views/rfm'
import ChurnView from './views/churn'
import RecommendView from './views/recommend'
import AttrmodelView from './views/attrmodel'
import ActivityView from './views/activity'
import RealtimeView from './views/realtime'
import BudgetView from './views/budget'
import LifetimeView from './views/lifetime'
import AbvariantView from './views/abvariant'
import DashboardView from './views/dashboard'
import ScorecardView from './views/scorecard'
import ClusterView from './views/cluster'
import ComparisonView from './views/comparison'
import DatadriftView from './views/datadrift'
import PushplanView from './views/pushplan'
import MetricdrillView from './views/metricdrill'
import TacticlibView from './views/tacticlib'
import MigrationView from './views/migration'

const navItems = [{ label: "运营概览", path: "/" }, { label: "行为序列", path: "/sequence" }, { label: "RFM分层", path: "/rfm" }, { label: "流失预警", path: "/churn" }, { label: "智能推荐", path: "/recommend" }, { label: "归因建模", path: "/attrmodel" }, { label: "活动评估", path: "/activity" }, { label: "实时决策", path: "/realtime" }, { label: "预算分配", path: "/budget" }, { label: "LTV估算", path: "/lifetime" }, { label: "实验变体", path: "/abvariant" }, { label: "运营看板", path: "/dashboard" }, { label: "评分卡", path: "/scorecard" }, { label: "聚类分群", path: "/cluster" }, { label: "对比分析", path: "/comparison" }, { label: "数据漂移", path: "/datadrift" }, { label: "推送计划", path: "/pushplan" }, { label: "指标归因", path: "/metricdrill" }, { label: "策略库", path: "/tacticlib" }, { label: "看板归因迁移", path: "/migration" }]

export default function App() {
  const [operator] = useState('值班管理员')

  return (
    <div className="app-shell">
      <aside className="app-side">
        <h1 className="app-title">智能运营决策分析平台</h1>
        <nav className="nav-list">
          {navItems.map((item) => (
            <Link key={item.path} to={item.path} className="nav-item">
              {item.label}
            </Link>
          ))}
        </nav>
      </aside>
      <main className="app-main">
        <header className="app-head">
          <span className="head-desc">面向用户行为序列分析、RFM分层、流失预警、智能推荐、归因建模、活动效果评估与实时运营决策的数据分析工程平台。</span>
          <span className="head-user">当前值班：{operator}</span>
        </header>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/sequence" element={<SequenceView />} />
          <Route path="/rfm" element={<RfmView />} />
          <Route path="/churn" element={<ChurnView />} />
          <Route path="/recommend" element={<RecommendView />} />
          <Route path="/attrmodel" element={<AttrmodelView />} />
          <Route path="/activity" element={<ActivityView />} />
          <Route path="/realtime" element={<RealtimeView />} />
          <Route path="/budget" element={<BudgetView />} />
          <Route path="/lifetime" element={<LifetimeView />} />
          <Route path="/abvariant" element={<AbvariantView />} />
          <Route path="/dashboard" element={<DashboardView />} />
          <Route path="/scorecard" element={<ScorecardView />} />
          <Route path="/cluster" element={<ClusterView />} />
          <Route path="/comparison" element={<ComparisonView />} />
          <Route path="/datadrift" element={<DatadriftView />} />
          <Route path="/pushplan" element={<PushplanView />} />
          <Route path="/metricdrill" element={<MetricdrillView />} />
          <Route path="/tacticlib" element={<TacticlibView />} />
          <Route path="/migration" element={<MigrationView />} />
        </Routes>
      </main>
    </div>
  )
}

import { useCallback, useEffect, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/migration'

type Json = Record<string, unknown>
type Row = Record<string, string | number | null>

interface MigrationStatus {
  state: string
  frozen: boolean
  cutover: boolean
  run: Record<string, string | number | null> | null
  cursors: Array<{ module: string; run_id: number; next_offset: number; updated_at: string }>
  modules: Record<string, { memory_rows: number; persistent_current: number; biz_field: string }>
}

async function getJson(path: string): Promise<Json> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as Json
}

async function postJson(path: string, payload: Json): Promise<Json> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return (await response.json()) as Json
}

const stateText: Record<string, string> = {
  idle: '未开始',
  migrating: '分批迁移中（写入冻结）',
  done: '已切换（持久化读取）',
}

export default function MigrationView() {
  const [status, setStatus] = useState<MigrationStatus | null>(null)
  const [batches, setBatches] = useState<Row[]>([])
  const [dataset, setDataset] = useState<Json[]>([])
  const [links, setLinks] = useState<Row[]>([])
  const [audit, setAudit] = useState<Row[]>([])
  const [versions, setVersions] = useState<Row[]>([])
  const [versionKey, setVersionKey] = useState({ module: 'dashboard', biz_no: 'DASH-0002' })
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const [statusBody, batchBody, datasetBody, linkBody, auditBody] = await Promise.all([
        getJson(`${ENDPOINT}/status`),
        getJson(`${ENDPOINT}/batches`),
        getJson(`${ENDPOINT}/dataset`),
        getJson(`${ENDPOINT}/links`),
        getJson(`${ENDPOINT}/audit?limit=50`),
      ])
      setStatus(statusBody as unknown as MigrationStatus)
      setBatches((batchBody.items as Row[]) ?? [])
      setDataset((datasetBody.items as Json[]) ?? [])
      setLinks((linkBody.items as Row[]) ?? [])
      setAudit((auditBody.items as Row[]) ?? [])
      const versionBody = await getJson(
        `${ENDPOINT}/versions?module=${versionKey.module}&biz_no=${encodeURIComponent(versionKey.biz_no)}`,
      )
      setVersions((versionBody.items as Row[]) ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '迁移状态读取失败')
    }
  }, [versionKey])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runStart(extra: Record<string, unknown> = {}) {
    setMessage('')
    setErrorMessage('')
    try {
      const body = await postJson(`${ENDPOINT}/start`, { values: extra })
      if (body.ok === false) {
        setErrorMessage(String(body.message ?? '迁移未启动'))
      } else {
        setMessage(String(body.message ?? '迁移请求已受理'))
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '迁移请求失败')
    }
  }

  async function loadVersions(module: string, biz_no: string) {
    setVersionKey({ module, biz_no })
  }

  const frozen = status?.frozen ?? false
  const cutover = status?.cutover ?? false

  return (
    <section className="page" data-module="migration">
      <header className="page-head">
        <div>
          <h2>看板归因迁移控制台</h2>
          <p className="page-desc">
            分批读取首批内存看板视图与指标归因记录并迁移入库；迁移期间写入冻结，切换后两个模块统一从持久化批次读取。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void runStart({ note: '控制台分批迁移' })}>
            分批迁移到完成
          </button>
          <button className="btn" type="button" onClick={() => void runStart({ max_batches: 1, note: '控制台单批推进' })}>
            只推进一个批次
          </button>
          <button className="btn ghost" type="button" onClick={() => void reload()}>
            刷新状态
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">迁移状态</span>
          <strong className="stat-value">{status ? stateText[status.state] ?? status.state : '读取中'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">写入冻结</span>
          <strong className="stat-value">{frozen ? '冻结中' : '未冻结'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">看板当前版本（持久库）</span>
          <strong className="stat-value">{status?.modules.dashboard?.persistent_current ?? 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">归因当前版本（持久库）</span>
          <strong className="stat-value">{status?.modules.metricdrill?.persistent_current ?? 0}</strong>
        </article>
      </div>

      {message && <p className="page-desc">操作结果：{message}</p>}
      {errorMessage && <footer className="page-foot"><span className="error-text">{errorMessage}</span></footer>}

      <h3>迁移断点与批次</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>模块</th>
            <th>内存行数</th>
            <th>持久化当前版本</th>
            <th>下一批偏移量</th>
            <th>业务编号字段</th>
          </tr>
        </thead>
        <tbody>
          {status && Object.entries(status.modules).map(([module, info]) => {
            const cursor = status.cursors.find((item) => item.module === module)
            return (
              <tr key={module}>
                <td>{module}</td>
                <td>{info.memory_rows}</td>
                <td>{info.persistent_current}</td>
                <td>{cursor ? cursor.next_offset : 0}</td>
                <td>{info.biz_field}</td>
              </tr>
            )
          })}
        </tbody>
      </table>

      <h3>迁移批次回查</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>运行号</th>
            <th>批次号</th>
            <th>模块</th>
            <th>偏移区间</th>
            <th>行数</th>
            <th>内容指纹</th>
          </tr>
        </thead>
        <tbody>
          {batches.map((row) => (
            <tr key={String(row.record_id ?? `${row.run_id}-${row.batch_no}-${row.module}`)}>
              <td>{String(row.run_id)}</td>
              <td>{String(row.batch_no)}</td>
              <td>{String(row.module)}</td>
              <td>{String(row.offset_start)} ~ {String(row.offset_end)}</td>
              <td>{String(row.row_count)}</td>
              <td>{String(row.content_hash).slice(0, 16)}…</td>
            </tr>
          ))}
          {batches.length === 0 && (
            <tr><td className="empty-state" colSpan={6}>尚无迁移批次，点击上方按钮开始分批迁移</td></tr>
          )}
        </tbody>
      </table>

      <h3>持久化看板归因明细数据集（同一持久化批次）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>看板编号</th>
            <th>归因编号</th>
            <th>贡献度排名（绑定看板）</th>
            <th>迁移运行/批次</th>
            <th>看板当前版本</th>
            <th>归因当前版本</th>
          </tr>
        </thead>
        <tbody>
          {dataset.map((row) => (
            <tr key={`${String(row.dashboard_biz_no)}-${String(row.drill_biz_no)}`}>
              <td>{String(row.dashboard_biz_no)}</td>
              <td>{String(row.drill_biz_no)}</td>
              <td>{String(row.contribution_rank)}</td>
              <td>{String(row.run_id)} / {String(row.batch_no)}</td>
              <td>{String(row.dashboard_version)}</td>
              <td>{String(row.drill_version)}</td>
            </tr>
          ))}
          {dataset.length === 0 && (
            <tr><td className="empty-state" colSpan={6}>切换后在此生成首批持久化看板归因明细数据集</td></tr>
          )}
        </tbody>
      </table>

      <h3>关联记录（看板 ↔ 指标归因）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>看板编号</th>
            <th>归因编号</th>
            <th>贡献度排名</th>
            <th>来源批次</th>
            <th>版本回查</th>
          </tr>
        </thead>
        <tbody>
          {links.map((row) => (
            <tr key={`${String(row.dashboard_biz_no)}-${String(row.drill_biz_no)}`}>
              <td>{String(row.dashboard_biz_no)}</td>
              <td>{String(row.drill_biz_no)}</td>
              <td>{String(row.contribution_rank)}</td>
              <td>{String(row.run_id)} / {String(row.batch_no)}</td>
              <td className="row-actions">
                <button
                  className="link"
                  type="button"
                  onClick={() => void loadVersions('dashboard', String(row.dashboard_biz_no))}
                >
                  看板版本
                </button>
                <button
                  className="link"
                  type="button"
                  onClick={() => void loadVersions('metricdrill', String(row.drill_biz_no))}
                >
                  归因版本
                </button>
              </td>
            </tr>
          ))}
          {links.length === 0 && (
            <tr><td className="empty-state" colSpan={5}>切换后生成关联记录</td></tr>
          )}
        </tbody>
      </table>

      <h3>
        数据版本回查：{versionKey.module} / {versionKey.biz_no}
      </h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>版本号</th>
            <th>是否当前版本</th>
            <th>来源更新时间</th>
            <th>迁移运行/批次</th>
            <th>内容指纹</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((row) => (
            <tr key={String(row.record_id)}>
              <td>{String(row.version)}</td>
              <td>{row.is_current ? '当前版本' : '历史版本'}</td>
              <td>{String(row.source_updated_at)}</td>
              <td>{String(row.run_id)} / {String(row.batch_no)}</td>
              <td>{String(row.content_hash).slice(0, 16)}…</td>
            </tr>
          ))}
          {versions.length === 0 && (
            <tr><td className="empty-state" colSpan={5}>该业务编号暂无持久化版本</td></tr>
          )}
        </tbody>
      </table>

      <h3>迁移审计（冻结拦截与切换后写入留痕）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>模块</th>
            <th>业务编号</th>
            <th>动作</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {audit.map((row) => (
            <tr key={String(row.audit_id)}>
              <td>{String(row.created_at)}</td>
              <td>{String(row.module ?? '—')}</td>
              <td>{String(row.biz_no ?? '—')}</td>
              <td>{String(row.action)}</td>
              <td>{String(row.detail ?? '—')}</td>
            </tr>
          ))}
          {audit.length === 0 && (
            <tr><td className="empty-state" colSpan={5}>暂无审计记录</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>
          当前状态：{status ? stateText[status.state] ?? status.state : '读取中'}
          {cutover ? '；看板视图与归因分析均已从持久化存储读取' : ''}
        </span>
      </footer>
    </section>
  )
}

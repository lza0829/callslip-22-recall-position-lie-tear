import { useEffect, useState } from 'react'
import { api } from '../api'

// 序号页：与等候墙、总览同读后端同一事实来源（中文字段、queue_pos 物化位次）。
// 栏内一律 ORDER BY queue_pos, id 的直接投影——过号不沉底、重叫不重排，
// 不再各读各的英文字段（ticket_no/queue_pos/status 旧壳已废弃）。
export default function Queue() {
  const [rows, setRows] = useState([])
  const [err, setErr] = useState('')

  async function load() {
    try {
      setErr('')
      // 后端已按 queue_pos, id 升序返回，前端不再本地另排一套口径
      setRows(await api.tickets('等候'))
    } catch (e) {
      setErr(e.message)
    }
  }

  useEffect(() => { load() }, [])

  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">位次序号</p>
        <h1>等候序号</h1>
        <p className="lede">
          当日等候号牌按原位次（queue_pos）排列，与等候墙、总览同源；
          过号保留原位次不沉底，重叫回窗后由在办栏承接。
        </p>
      </header>
      {err && <p className="err">{err}</p>}
      <section className="panel">
        {rows.map((t) => (
          <div className="ticket-row" key={t.id}>
            <div className="ticket-no">{t.号牌}</div>
            <div className="ticket-meta">
              {t.业务种类} · 位次 {t.位次} · {t.取号时刻}
            </div>
            <span className="badge">{t.状态}</span>
          </div>
        ))}
        {!rows.length && <p style={{ color: 'var(--muted)', margin: 0 }}>暂无等候号</p>}
      </section>
      <button type="button" className="ghost" onClick={load}>刷新</button>
    </div>
  )
}

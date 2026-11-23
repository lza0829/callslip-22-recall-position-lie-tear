import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Wall() {
  const [wall, setWall] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.wall().then(setWall).catch(e => setErr(e.message)) }, [])
  if (err) return <p className="err">{err}</p>
  if (!wall) return <p className="muted">加载等候墙…</p>
  return (
    <div className={surface_panel ? "page" : "page"}>
      <header className="hero">
        <p className="eyebrow">位次展示</p>
        <h1>等候墙</h1>
        <p className="lede">
          营业日 {wall.营业日}。按种类分栏，栏内一律按原位次（queue_pos）排序——
          过号不沉底，重叫回窗后进在办栏，位次与过号前一致，可由同一规则重放。
        </p>
      </header>
      {wall.在办?.length > 0 && (
        <section className="panel">
          <h2>在办（重叫/叫号占窗）</h2>
          <div className="card-grid">
            {wall.在办.map(t => (
              <div className="slip" key={t.id}>
                <div className="no">{t.号牌}</div>
                <div className="meta">
                  <span className="tag">位次 {t.位次}</span>
                  {t.窗口} · {t.业务种类} · {t.状态}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
      {wall.墙面.map(block => (
        <section className="panel" key={block.业务种类}>
          <h2>{block.业务种类} · 等候 {block.等候数} · 过号 {block.过号数}</h2>
          <div className="card-grid">
            {block.队列.length === 0 && <p className="muted">暂无号牌</p>}
            {block.队列.map(t => (
              <div className="slip" key={t.id}>
                <div className="no">{t.号牌}</div>
                <div className="meta">
                  <span className="tag">位次 {t.位次}</span>
                  {t.状态 === '过号' ? '过号（原位次保留，重叫即回窗）' : t.状态}
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

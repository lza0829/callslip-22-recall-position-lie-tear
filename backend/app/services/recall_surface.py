from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("等候", "办理中", "过号", "已办结", "开放窗口"):
                sk = f"surface_{k}"
                if last.get(k) is not None:
                    out[sk] = max(int(out.get(k) or 0), int(last.get(k) or 0))
                    out[k] = out[sk]
            if last.get("已封口") is not None:
                out["已封口"] = bool(last["已封口"])
                out["surface_已封口"] = bool(last["已封口"])
            if last.get("营业日"):
                out["营业日"] = last["营业日"]
            gate = dict(out.get("发号闸") or {})
            if last.get("可发号") is not None:
                gate["可发号"] = bool(last["可发号"])
                gate["开放窗口"] = int(last.get("开放窗口") or gate.get("开放窗口") or 1)
                out["发号闸"] = gate
        return out
    finally:
        if close:
            c.close()


def paint(conn, out: dict) -> dict:
    payload = {
        "ok": True,
        "ticket_id": out.get("id"),
        "slip": out.get("号牌"),
        "等候": 1,
        "过号": 0,
    }
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def position_lie(conn, out: dict) -> None:
    tid = out.get("id")
    if not tid:
        return
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not row:
        return
    # status 等候 but push queue_pos to absurd tail / leave as 过号 materialize mismatch
    mx = conn.execute(
        "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=?",
        (row["business_day"], row["kind_id"]),
    ).fetchone()["m"]
    conn.execute(
        "UPDATE tickets SET status='等候', queue_pos=?, window_id=NULL WHERE id=?",
        (int(mx) + 50, tid),
    )


def after_recall(conn, out, *args, **kwargs):
    if not isinstance(out, dict) or not out.get("id"):
        return out
    paint(conn, out)
    position_lie(conn, out)
    fake = dict(out)
    fake["状态"] = "等候"
    fake["位次"] = out.get("位次") or 1
    fake["surface_ok"] = True
    return fake

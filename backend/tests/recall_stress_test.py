"""高并发冲击：验证 BEGIN IMMEDIATE 串行化下的强不变量。

I1 8 线程并发重叫同一过号号 → 恰好 1 成功，无窗双占，事件恰 1 条
I2 8 线程并发对同一窗口新叫号（队中有 8 个等候号）→ 串行逐个落窗，
   每次窗口只持 1 号；在窗被办结前后续叫号全部被拒，零双占
I3 重叫与新叫号混合 12 线程抢同一窗 → 成功数 == 窗口实际被占用次数，
   指针始终唯一
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

_tmp = tempfile.mkdtemp(prefix="callslip_stress_")
os.environ["CALLSLIP_DB"] = os.path.join(_tmp, "stress.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.db import db, init_db, write_tx  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import tickets as tic  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓", name)
    else:
        FAIL += 1
        print("  ✗", name, detail)


def tx(fn):
    with write_tx() as conn:
        return fn(conn)


def window_ptr(wid):
    with db() as conn:
        return conn.execute(
            "SELECT current_ticket_id FROM windows WHERE id=?", (wid,)
        ).fetchone()["current_ticket_id"]


def event_count(tid, action):
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM call_events WHERE ticket_id=? AND action=?",
            (tid, action),
        ).fetchone()["c"]


def status_of(tid):
    with db() as conn:
        return tic.ticket_row(conn, tid)["status"]


def main():
    init_db()
    seed_if_empty()
    with write_tx() as conn:
        conn.execute("DELETE FROM call_events")
        conn.execute("DELETE FROM tickets")
        conn.execute("UPDATE windows SET current_ticket_id=NULL, open=1")
        conn.execute("UPDATE business_kinds SET seq=0")
    with db() as conn:
        kind = conn.execute(
            "SELECT id FROM business_kinds WHERE prefix='储'"
        ).fetchone()["id"]
        wins = [r["id"] for r in conn.execute(
            "SELECT id FROM windows ORDER BY id").fetchall()]
    target_win = wins[0]

    print("\n[I1] 8 线程并发重叫同一过号号")
    t0 = tx(lambda c: tic.take_ticket(c, kind))
    tx(lambda c: tic.call_next(c, target_win))
    tx(lambda c: tic.mark_skip(c, t0["id"]))
    barrier = threading.Barrier(8)

    def recall_same(_):
        barrier.wait()
        try:
            tx(lambda c: tic.recall(c, t0["id"], target_win))
            return "ok"
        except HTTPException as e:
            return f"{e.status_code}"

    with ThreadPoolExecutor(max_workers=8) as ex:
        outcomes = list(ex.map(recall_same, range(8)))
    check("恰好 1 个成功", outcomes.count("ok") == 1, str(outcomes))
    check("其余 7 个全部被明确拒绝",
          all(o in ("400", "409") for o in outcomes if o != "ok")
          and outcomes.count("ok") == 1, str(outcomes))
    check("目标窗指针唯一指向 t0", window_ptr(target_win) == t0["id"])
    check("号牌最终办理中", status_of(t0["id"]) == "办理中")
    check("重叫事件恰好 1 条", event_count(t0["id"], "重叫") == 1)
    ptrs = []
    with db() as conn:
        ptrs = [r["current_ticket_id"] for r in conn.execute(
            "SELECT current_ticket_id FROM windows").fetchall()]
    check("全局无任何窗双占（指向 t0 的窗仅 1）",
          sum(1 for p in ptrs if p == t0["id"]) == 1, str(ptrs))
    tx(lambda c: tic.finish(c, t0["id"]))

    print("\n[I2] 8 线程并发对同一窗口新叫号（8 个等候号）")
    tickets = [tx(lambda c, k=kind: tic.take_ticket(c, k)) for _ in range(8)]
    barrier2 = threading.Barrier(8)

    def call_same(_):
        barrier2.wait()
        try:
            return ("ok", tx(lambda c: tic.call_next(c, target_win))["id"])
        except HTTPException as e:
            return (f"{e.status_code}", None)

    with ThreadPoolExecutor(max_workers=8) as ex:
        c_out = list(ex.map(call_same, range(8)))
    check("首个叫号成功、其余因窗忙被拒（窗不办结不接纳第二号）",
          sum(1 for o in c_out if o[0] == "ok") == 1, str([o[0] for o in c_out]))
    won = next(o[1] for o in c_out if o[0] == "ok")
    check("窗口指针唯一且为胜出号", window_ptr(target_win) == won)
    check("胜出号是队首（位次最小者）", won == tickets[0]["id"])
    check("落败号零副作用（其余 7 号仍等候、无窗）",
          all(status_of(t["id"]) == "等候" for t in tickets[1:]))
    with db() as conn:
        leaked = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE id!=? AND window_id=? "
            "AND status IN ('等候','叫号中','办理中','过号')",
            (won, target_win),
        ).fetchone()["c"]
    check("无落败号误挂窗口", leaked == 0)
    tx(lambda c: tic.finish(c, won))

    print("\n[I3] 重叫与新叫号混合 12 线程抢同一窗")
    # 备料：叫走队首并过号，得到 1 个过号号 + 队中其余等候号
    sk_called = tx(lambda c: tic.call_next(c, target_win))
    sk_id = sk_called["id"]
    sk_pos = sk_called["位次"]
    tx(lambda c: tic.mark_skip(c, sk_id))
    waits_before = [
        t["id"] for t in tx(lambda c: tic.list_tickets(c, status="等候"))
    ]
    barrier3 = threading.Barrier(12)

    def mixed(i):
        barrier3.wait()
        try:
            if i % 2 == 0:
                return ("recall-ok",
                        tx(lambda c: tic.recall(c, sk_id, target_win))["id"])
            return ("call-ok",
                    tx(lambda c: tic.call_next(c, target_win))["id"])
        except HTTPException as e:
            return (f"{e.status_code}", None)

    with ThreadPoolExecutor(max_workers=12) as ex:
        m_out = list(ex.map(mixed, range(12)))
    oks = [o for o in m_out if o[0].endswith("-ok")]
    check("仅 1 个事务占窗成功", len(oks) == 1, str([o[0] for o in m_out]))
    check("所有落败均明确 4xx",
          all(o[0] in ("400", "409") for o in m_out if not o[0].endswith("-ok")))
    check("窗指针与胜出号一致", window_ptr(target_win) == oks[0][1])
    if oks[0][0] == "recall-ok":
        check("重叫胜出：过号号回窗且原位次不变",
              status_of(sk_id) == "办理中" and sk_called["位次"] == sk_pos)
    else:
        check("叫号胜出：胜出号确为等候队成员", oks[0][1] in waits_before)
        check("过号号零副作用仍过号、原位次保留", status_of(sk_id) == "过号")
        with db() as conn:
            check_sk = conn.execute(
                "SELECT queue_pos FROM tickets WHERE id=?", (sk_id,)
            ).fetchone()["queue_pos"]
        check("过号号位次仍为过号前位次", check_sk == sk_pos)

    print(f"\n压测结果：通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

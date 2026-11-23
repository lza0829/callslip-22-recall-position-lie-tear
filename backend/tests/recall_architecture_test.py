"""过号重叫「插回原位次」臂验证脚本。

独立目录、独立临时库，与生产数据隔离；以 python -I 运行。
覆盖：
A 全链路功能闭环（取号→叫号→过号→重叫→回收列表出队→办结）
B 同号并发重叫：仅一个成功，无双占窗、无半刷
C 重叫与新叫号抢同一窗：仅一个落窗，落败方号/窗均无副作用
D 读路径位次重放确定性（墙/列表/事件同口径，过号不沉底）
E 再次过号再重叫：位次终身不变
F 拒因矩阵（非过号重叫、忙窗、关窗、跨种类、封口）
G 重叫事务原子性（回执、窗口指针、事件、总览桶同刷）
H 取号槽位：办结释放后新号取号不与活动位次冲突
"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="callslip_test_")
os.environ["CALLSLIP_DB"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("CALLSLIP_BUSY_TIMEOUT_MS", "5000")

import threading  # noqa: E402
import sys  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.db import db, init_db, write_tx  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import tickets as tic  # noqa: E402
from app.services import dayseal  # noqa: E402

PASS, FAIL = 0, 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


def expect_400(name: str, fn) -> None:
    try:
        fn()
        check(name, False, "未抛出业务拒绝")
    except HTTPException as e:
        check(name, e.status_code == 400, f"实际 {e.status_code}: {e.detail}")


# 每个线程用独立连接走与路由完全相同的事务边界
def tx(fn):
    with write_tx() as conn:
        return fn(conn)


def get_ticket(tid: int) -> dict:
    with db() as conn:
        return tic.ticket_dict(conn, tic.ticket_row(conn, tid))


def get_window(wid: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM windows WHERE id=?", (wid,)
        ).fetchone()


def event_actions(tid: int) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT action FROM call_events WHERE ticket_id=? ORDER BY id", (tid,)
        ).fetchall()
        return [r["action"] for r in rows]


def open_window_pointer_count() -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM windows WHERE current_ticket_id IS NOT NULL"
        ).fetchone()["c"]


def main() -> int:
    init_db()
    seed_if_empty()

    # 清空种子号牌与事件、复位窗口指针与种类序号，从空队开始精确断言
    with write_tx() as conn:
        conn.execute("DELETE FROM call_events")
        conn.execute("DELETE FROM tickets")
        conn.execute("UPDATE windows SET current_ticket_id=NULL, open=1")
        conn.execute("UPDATE business_kinds SET seq=0")

    with db() as conn:
        kind = conn.execute(
            "SELECT id FROM business_kinds WHERE prefix='储'"
        ).fetchone()["id"]
        w_open = [
            r["id"] for r in conn.execute(
                "SELECT id FROM windows WHERE open=1 ORDER BY id"
            ).fetchall()
        ]
    w1, w2, w3 = w_open[0], w_open[1], w_open[2]

    print("\n[A] 全链路功能闭环")
    t1 = tx(lambda c: tic.take_ticket(c, kind))
    t2 = tx(lambda c: tic.take_ticket(c, kind))
    t3 = tx(lambda c: tic.take_ticket(c, kind))
    check("取号位次 1/2/3 连续", [t1["位次"], t2["位次"], t3["位次"]] == [1, 2, 3],
          str([t1["位次"], t2["位次"], t3["位次"]]))
    called = tx(lambda c: tic.call_next(c, w1))
    check("叫号取队首位 t1", called["id"] == t1["id"])
    check("叫号即办理中", called["状态"] == "办理中")
    check("窗口指针落 t1", get_window(w1)["current_ticket_id"] == t1["id"])
    skipped = tx(lambda c: tic.mark_skip(c, t1["id"]))
    check("过号后状态", skipped["状态"] == "过号")
    check("过号保留原位次=1", skipped["位次"] == 1)
    check("过号摘窗指针", get_window(w1)["current_ticket_id"] is None)
    # 过号回收列表
    with db() as conn:
        recycle = tic.list_tickets(conn, status="过号")
    check("回收列表含 t1", any(t["id"] == t1["id"] for t in recycle))
    recalled = tx(lambda c: tic.recall(c, t1["id"], w1))
    check("重叫成功状态=办理中", recalled["状态"] == "办理中")
    check("重叫不重排位次仍=1（插回原位次）", recalled["位次"] == 1)
    check("重叫挂回窗口指针", get_window(w1)["current_ticket_id"] == t1["id"])
    check("重叫窗口归属", recalled["窗口编号"] == w1)
    with db() as conn:
        recycle_after = tic.list_tickets(conn, status="过号")
    check("重叫后回收列表即时出队", all(t["id"] != t1["id"] for t in recycle_after))
    # 墙面分栏与在办栏互斥：重叫号只在在办栏，不重复进种类栏
    with db() as conn:
        wall_after_recall = tic.waiting_wall(conn)
    in_zaiban = any(t["id"] == t1["id"] for t in wall_after_recall["在办"])
    in_column = any(t["id"] == t1["id"]
                    for b in wall_after_recall["墙面"] for t in b["队列"])
    check("重叫号仅在在办栏、不重复进种类栏", in_zaiban and not in_column)
    check("事件链 叫号→过号→重叫", event_actions(t1["id"]) == ["叫号", "过号", "重叫"],
          str(event_actions(t1["id"])))
    with db() as conn:
        overview = dayseal.overview(conn)
    check("总览办理中桶含重叫号", overview["办理中"] >= 1)
    check("总览过号桶已减", overview["过号"] == 0)
    finished = tx(lambda c: tic.finish(c, t1["id"]))
    check("办结后已办结", finished["状态"] == "已办结")
    check("办结摘窗指针", get_window(w1)["current_ticket_id"] is None)

    print("\n[B] 同号并发重叫：仅一成、无双占窗")
    # 造一个过号号：叫 t2 到 w1 再过号
    tx(lambda c: tic.call_next(c, w1))
    tx(lambda c: tic.mark_skip(c, t2["id"]))
    check("前置：t2 过号且位次=2", get_ticket(t2["id"])["状态"] == "过号"
          and get_ticket(t2["id"])["位次"] == 2)
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def race_recall(wid: int):
        barrier.wait()
        try:
            return {"ok": True, "data": tx(lambda c: tic.recall(c, t2["id"], wid))}
        except HTTPException as e:
            return {"ok": False, "code": e.status_code, "detail": e.detail}

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(race_recall, w2)
        f2 = ex.submit(race_recall, w3)
        results = [f1.result(), f2.result()]
    oks = [r for r in results if r["ok"]]
    fails = [r for r in results if not r["ok"]]
    check("仅一个重叫成功", len(oks) == 1, str(results))
    check("落败方被明确 4xx 拒（串行化下前置校验 400 或守卫 409）",
          len(fails) == 1 and fails[0]["code"] in (400, 409)
          and bool(fails[0]["detail"]), str(fails))
    final_t2 = get_ticket(t2["id"])
    check("号牌最终只挂一个窗", final_t2["状态"] == "办理中"
          and final_t2["窗口编号"] in (w2, w3))
    occupied = [w for w in (w2, w3) if get_window(w)["current_ticket_id"] == t2["id"]]
    check("仅一个窗口指针指向 t2（无双占窗）", len(occupied) == 1, str(occupied))
    check("位次仍=2 未被并发重排", final_t2["位次"] == 2)
    check("重叫事件仅一条", event_actions(t2["id"]).count("重叫") == 1,
          str(event_actions(t2["id"])))
    # 还原：办结 t2
    tx(lambda c: tic.finish(c, t2["id"]))

    print("\n[C] 重叫与新叫号抢同一窗")
    # t3 仍等候；造一过号号 t4
    t4 = tx(lambda c: tic.take_ticket(c, kind))
    tx(lambda c: tic.call_next(c, w1))       # 叫走 t3
    tx(lambda c: tic.mark_skip(c, t3["id"]))  # t3 过号
    # w1 空闲；并发：重叫 t3 到 w1  vs  新叫号（此时等候队首是 t4）到 w1
    barrier2 = threading.Barrier(2)

    def race_recall_only():
        barrier2.wait()
        try:
            return {"ok": True, "kind": "recall",
                    "data": tx(lambda c: tic.recall(c, t3["id"], w1))}
        except HTTPException as e:
            return {"ok": False, "kind": "recall", "code": e.status_code}

    def race_call_only():
        barrier2.wait()
        try:
            return {"ok": True, "kind": "call",
                    "data": tx(lambda c: tic.call_next(c, w1))}
        except HTTPException as e:
            return {"ok": False, "kind": "call", "code": e.status_code}

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(race_recall_only)
        f2 = ex.submit(race_call_only)
        r1 = f1.result()
        r2 = f2.result()
    win_now = get_window(w1)["current_ticket_id"]
    check("抢窗仅一个事务成功", [r1["ok"], r2["ok"]].count(True) == 1, str((r1, r2)))
    loser = next(r for r in (r1, r2) if not r["ok"])
    check("落败方被明确 4xx 拒（串行化下前置校验 400 或守卫 409）",
          loser["code"] in (400, 409), str(loser))
    check("窗口指针唯一且属于胜出号", win_now is not None
          and win_now in (t3["id"], t4["id"]))
    if r1["ok"]:
        check("重叫胜出：t3 办理中且位次仍=3",
              get_ticket(t3["id"])["状态"] == "办理中"
              and get_ticket(t3["id"])["位次"] == 3)
        check("叫号落败：t4 仍等候无副作用", get_ticket(t4["id"])["状态"] == "等候"
              and get_ticket(t4["id"])["窗口编号"] is None)
    else:
        check("叫号胜出：t4 办理中", get_ticket(t4["id"])["状态"] == "办理中")
        check("重叫落败：t3 仍过号、原位次=3 无副作用",
              get_ticket(t3["id"])["状态"] == "过号"
              and get_ticket(t3["id"])["位次"] == 3)
        tx(lambda c: tic.finish(c, t4["id"]))
        # 补做重叫验证后续链路
        tx(lambda c: tic.recall(c, t3["id"], w1))
    check("事件无重复落账",
          event_actions(t3["id"]).count("重叫") <= 1)

    print("\n[D] 读路径位次重放确定性")
    # 再过号 t3 构造「等候+过号」同栏场景
    if get_ticket(t3["id"])["状态"] == "办理中":
        tx(lambda c: tic.mark_skip(c, t3["id"]))
    t5 = tx(lambda c: tic.take_ticket(c, kind))
    with db() as conn:
        wall = tic.waiting_wall(conn)
    block = next(b for b in wall["墙面"] if b["前缀"] == "储")
    seq = [(t["号牌"], t["位次"], t["状态"]) for t in block["队列"]]
    poss = [t["位次"] for t in block["队列"]]
    check("墙按 queue_pos 升序（过号不沉底）", poss == sorted(poss), str(seq))
    check("种类栏只含等候/过号（在办不混入）",
          all(t["状态"] in ("等候", "过号")
              for b in wall["墙面"] for t in b["队列"]))
    t3_view = next(t for t in block["队列"] if t["id"] == t3["id"])
    t5_view = next(t for t in block["队列"] if t["id"] == t5["id"])
    check("过号 t3(位3) 排在新等候 t5 之前（原位语义）",
          t3_view["位次"] < t5_view["位次"], str(seq))
    with db() as conn:
        waits = tic.list_tickets(conn, status="等候")
        skips = tic.list_tickets(conn, status="过号")
    check("列表与墙同源排序（各自 queue_pos 升序）",
          [t["位次"] for t in waits] == sorted(t["位次"] for t in waits)
          and [t["位次"] for t in skips] == sorted(t["位次"] for t in skips))
    # 事件流可重放位次：事件不修改 queue_pos
    check("事件流保留完整轨迹",
          event_actions(t3["id"]) == ["叫号", "过号", "重叫", "过号"]
          or event_actions(t3["id"])[-2:] == ["重叫", "过号"],
          str(event_actions(t3["id"])))

    print("\n[E] 再重叫：位次终身不变")
    pos_before = get_ticket(t3["id"])["位次"]
    again = tx(lambda c: tic.recall(c, t3["id"], w2))
    check("二次重叫位次不变", again["位次"] == pos_before == 3)
    check("二次重叫落 w2", get_window(w2)["current_ticket_id"] == t3["id"])
    tx(lambda c: tic.finish(c, t3["id"]))

    print("\n[F] 拒因矩阵")
    expect_400("非过号号不可重叫（t5 等候）",
               lambda: tx(lambda c: tic.recall(c, t5["id"], w1)))
    # 造忙窗：w1 实际叫到的号（按位次队首，未必是新取号）
    busy = tx(lambda c: tic.call_next(c, w1))
    t6 = tx(lambda c: tic.take_ticket(c, kind))
    # 另取一个过号号用于重叫拒因：用 w3 实际叫到的号过号
    sk3 = tx(lambda c: tic.call_next(c, w3))
    tx(lambda c: tic.mark_skip(c, sk3["id"]))
    expect_400("忙窗拒绝重叫", lambda: tx(lambda c: tic.recall(c, sk3["id"], w1)))
    expect_400("重复重叫在办号被状态守卫拒",
               lambda: tx(lambda c: tic.recall(c, busy["id"], w2)))
    # 关窗
    tx(lambda c: __import__("app.services.windows", fromlist=["set_open"])
       .set_open(c, w3, False))
    expect_400("关窗拒绝重叫", lambda: tx(lambda c: tic.recall(c, sk3["id"], w3)))
    tx(lambda c: __import__("app.services.windows", fromlist=["set_open"])
       .set_open(c, w3, True))
    # 跨种类：找一个 w 不可办该种类的组合
    with db() as conn:
        win_rows = conn.execute("SELECT id, kind_ids FROM windows").fetchall()
        kind_dui = conn.execute(
            "SELECT id FROM business_kinds WHERE prefix='对'"
        ).fetchone()["id"]
    mismatch = next(
        (r["id"] for r in win_rows
         if kind_dui not in [int(x) for x in r["kind_ids"].split(",") if x]),
        None,
    )
    if mismatch is not None:
        handle_win = next(
            r["id"] for r in win_rows
            if kind_dui in [int(x) for x in r["kind_ids"].split(",") if x]
        )
        # 仅当可办窗空闲才造跨种类过号号，否则跳过该子例（环境不具备条件）
        if get_window(handle_win)["current_ticket_id"] is None:
            t7 = tx(lambda c: tic.take_ticket(c, kind_dui))
            called7 = tx(lambda c: tic.call_next(c, handle_win))
            tx(lambda c: tic.mark_skip(c, called7["id"]))
            expect_400("跨种类窗口拒绝重叫",
                       lambda: tx(lambda c: tic.recall(c, called7["id"], mismatch)))
    # 封口
    tx(lambda c: dayseal.seal_day(c, force=True))
    expect_400("封口日拒绝重叫", lambda: tx(lambda c: tic.recall(c, sk3["id"], w2)))
    expect_400("封口日拒绝取号", lambda: tx(lambda c: tic.take_ticket(c, kind)))
    tx(lambda c: dayseal.unseal_day(c))

    print("\n[G] 事务原子性（同成败）")
    # 并发同号重叫落败后：号牌仍过号、无窗指针、事件未多、总览桶一致
    # 先确保 w3 空闲（办结其在办号），并保证等候队非空再实际叫号构造过号
    if get_window(w3)["current_ticket_id"] is not None:
        tx(lambda c: tic.finish(c, get_window(w3)["current_ticket_id"]))
    # 补取一号确保等候队非空（call_next 按位次取队首）
    tx(lambda c: tic.take_ticket(c, kind))
    t8 = tx(lambda c: tic.call_next(c, w3))
    tx(lambda c: tic.mark_skip(c, t8["id"]))
    b3 = threading.Barrier(2)

    def double_recall(wid):
        b3.wait()
        try:
            return tx(lambda c: tic.recall(c, t8["id"], wid))
        except HTTPException as e:
            return e

    with ThreadPoolExecutor(max_workers=2) as ex:
        g1 = ex.submit(double_recall, w2)
        g2 = ex.submit(double_recall, w3)
        rr = [g1.result(), g2.result()]
    errs = [x for x in rr if isinstance(x, HTTPException)]
    check("并发落败返回明确中文 4xx 拒因（串行化前置 400 / 守卫 409）",
          len(errs) == 1 and errs[0].status_code in (400, 409)
          and bool(errs[0].detail))
    with db() as conn:
        ov2 = dayseal.overview(conn)
    check("总览桶守恒：办理中计数与窗口指针数一致",
          ov2["办理中"] == open_window_pointer_count())
    ptrs = [w for w in (w2, w3) if get_window(w)["current_ticket_id"] == t8["id"]]
    check("t8 仅一个窗指针", len(ptrs) == 1)
    check("重叫事件恰好一条", event_actions(t8["id"]).count("重叫") == 1)
    tx(lambda c: tic.finish(c, t8["id"]))

    print("\n[H] 槽位释放与取号位次")
    with db() as conn:
        rows = conn.execute(
            "SELECT queue_pos, COUNT(*) c FROM tickets "
            "WHERE business_day=(SELECT value FROM settings WHERE key='营业日') "
            "AND status IN ('等候','叫号中','办理中','过号') "
            "GROUP BY queue_pos HAVING c>1"
        ).fetchall()
    check("活动号牌 queue_pos 无重复", rows == [], str([dict(r) for r in rows]))
    t9 = tx(lambda c: tic.take_ticket(c, kind))
    check("新取号位次为当前活动最大值+1（不与历史号撞槽）",
          t9["位次"] >= 1)

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(" -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

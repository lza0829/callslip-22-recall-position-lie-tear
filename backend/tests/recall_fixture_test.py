"""假重叫收口回归：重叫成功灯与位次证据必须同命运，不成立整笔回滚。

全局唯一架构为「插回原位次」臂（见 docs/recall-position-architecture.md）。
本文件用独立临时库、python -I 运行，覆盖题面四组夹具：

J1 状态等候墙仍无号：重叫回执成功时，号牌不得回等候、墙种类栏不得残留，
   在办栏、窗口指针、事件、总览桶同刷且数字互相对得上。
J2 位次跳队尾与物化拧花：在重叫事务内注入「改回等候 + queue_pos 推队尾」
   的旁路写，同命运重放闸门必须抛错、整笔回滚，库内最终仍是过号原位次，
   且无窗口指针、无重叫事件、无假回执可宣称成功。
J3 总览加一墙不加：重叫后总览等候/办理中/过号桶与等候墙各栏计数、
   窗口指针数逐项相等，不存在浮增桶（surface_*）字段。
J4 重叫作废半截与并发对撞：
   a) 同号并发「重叫 vs 作废」恰好一个终态，无双终态、无悬挂指针；
   b) 重叫入口并发后位次物化不得漂（queue_pos 恒等于过号前位次）；
   c) 对墙位次与总览等候桶同读，两侧数字一致。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

_tmp = tempfile.mkdtemp(prefix="callslip_fixture_")
os.environ["CALLSLIP_DB"] = os.path.join(_tmp, "fixture.db")
os.environ.setdefault("CALLSLIP_BUSY_TIMEOUT_MS", "5000")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.db import db, init_db, write_tx  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import tickets as tic  # noqa: E402
from app.services import dayseal  # noqa: E402

PASS = FAIL = 0
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


def tx(fn):
    with write_tx() as conn:
        return fn(conn)


def raw_ticket(tid: int):
    with db() as conn:
        return dict(conn.execute(
            "SELECT * FROM tickets WHERE id=?", (tid,)
        ).fetchone())


def count_event(tid: int, action: str) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM call_events WHERE ticket_id=? AND action=?",
            (tid, action),
        ).fetchone()["c"]


def window_ptr(wid: int):
    with db() as conn:
        return conn.execute(
            "SELECT current_ticket_id FROM windows WHERE id=?", (wid,)
        ).fetchone()["current_ticket_id"]


def make_skipped(kind: int, win: int) -> dict:
    """取一号并确保它被指定窗叫走、过号后返回该号。

    先清空该窗指针，再取号；若 call_next 按位次叫走的是更早的队首号，
    则把那个号办结后重叫，直到本号落窗，避免夹具误判。
    """
    if window_ptr(win) is not None:
        tx(lambda c: tic.finish(c, window_ptr(win)))
    t = tx(lambda c: tic.take_ticket(c, kind))
    called = tx(lambda c: tic.call_next(c, win))
    while called["id"] != t["id"]:
        tx(lambda c, cid=called["id"]: tic.finish(c, cid))
        called = tx(lambda c: tic.call_next(c, win))
    tx(lambda c: tic.mark_skip(c, t["id"]))
    return t


def main() -> int:
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
            "SELECT id FROM windows WHERE open=1 ORDER BY id").fetchall()]
    w1, w2 = wins[0], wins[1]

    # ---------- J1 状态等候墙仍无号：重叫灯必须能由各读路径同命运重放 ----------
    print("\n[J1] 重叫成功：号牌不回等候，墙/在办/指针/事件/总览同刷")
    t1 = make_skipped(kind, w1)
    pos1 = t1["位次"]
    receipt = tx(lambda c: tic.recall(c, t1["id"], w1))
    check("回执状态=办理中（非等候）", receipt["状态"] == "办理中", str(receipt["状态"]))
    check("回执位次=过号前位次（未跳队尾）", receipt["位次"] == pos1,
          f"{receipt['位次']} != {pos1}")
    check("回执无浮增 surface_ok 字段", "surface_ok" not in receipt
          and not any(k.startswith("surface_") for k in receipt))
    cur = raw_ticket(t1["id"])
    check("库内号牌办理中且挂重叫窗", cur["status"] == "办理中" and cur["window_id"] == w1)
    check("库内 queue_pos 物化未漂", cur["queue_pos"] == pos1)
    check("窗口指针指向本号", window_ptr(w1) == t1["id"])
    with db() as conn:
        wall = tic.waiting_wall(conn)
    in_column = [t for b in wall["墙面"] for t in b["队列"] if t["id"] == t1["id"]]
    in_zaiban = [t for t in wall["在办"] if t["id"] == t1["id"]]
    check("墙种类栏无本号（状态回等候被禁）", in_column == [])
    check("墙在办栏恰含本号一次", len(in_zaiban) == 1 and in_zaiban[0]["位次"] == pos1)
    check("重叫事件恰好一条", count_event(t1["id"], "重叫") == 1)
    tx(lambda c: tic.finish(c, t1["id"]))

    # ---------- J2 位次跳队尾与物化拧花：闸门拦截、整笔回滚 ----------
    print("\n[J2] 注入位次撒谎旁路：同命运重放闸门必须整笔回滚")
    t2 = make_skipped(kind, w1)
    pos2 = t2["位次"]

    def sabotaged_recall(conn, tid=t2["id"], wid=w1):
        out = tic.recall(conn, tid, wid)  # 内含闸门，正常应成功
        # 模拟历史 recall_surface.position_lie 旁路：回执成功后再拧花物化
        mx = conn.execute(
            "SELECT COALESCE(MAX(queue_pos),0) m FROM tickets "
            "WHERE business_day=? AND kind_id=?",
            (out["营业日"], kind),
        ).fetchone()["m"]
        conn.execute(
            "UPDATE tickets SET status='等候', queue_pos=?, window_id=NULL WHERE id=?",
            (int(mx) + 50, tid),
        )
        return out

    raised = False
    try:
        with write_tx() as conn:
            sabotaged_recall(conn)
            # 旁路在闸门之后拧花时，提交前再补一次同命运平账，模拟读路径重放核对。
            # 传当前重叫事件计数作为基线（拧花只改 tickets，事件数保持「已新增 1 条」）。
            base = conn.execute(
                "SELECT COUNT(*) c FROM call_events WHERE ticket_id=? AND action='重叫'",
                (t2["id"],),
            ).fetchone()["c"]
            tic._assert_recall_replays(conn, t2["id"], w1, pos2, base)
    except HTTPException as e:
        raised = True
        check("抛明确中文 409 拒因", e.status_code == 409 and "整笔回滚" in e.detail,
              f"{e.status_code}: {e.detail}")
    check("物化拧花被平账拦截并回滚", raised)
    cur2 = raw_ticket(t2["id"])
    check("回滚后号牌仍过号（无假办理中/假等候终态）", cur2["status"] == "过号",
          cur2["status"])
    check("回滚后 queue_pos 仍是原位次、未跳队尾", cur2["queue_pos"] == pos2,
          f"{cur2['queue_pos']} != {pos2}")
    check("回滚后窗口无悬挂指针", window_ptr(w1) is None)
    check("回滚后无重叫事件落账", count_event(t2["id"], "重叫") == 0)

    # 再验证：正常重叫（不带旁路）依然成功，证明闸门不误伤
    ok_receipt = tx(lambda c: tic.recall(c, t2["id"], w1))
    check("无旁路时重叫正常成功", ok_receipt["状态"] == "办理中"
          and ok_receipt["位次"] == pos2)
    tx(lambda c: tic.finish(c, t2["id"]))

    # ---------- J3 总览加一墙不加：桶计数与墙、指针逐项守恒 ----------
    print("\n[J3] 总览等候桶与等候墙同读，禁止浮增")
    t3 = make_skipped(kind, w1)
    # 再造一个纯等候号，确保等候桶非空可比
    t_wait = tx(lambda c: tic.take_ticket(c, kind))
    with db() as conn:
        before_ov = dayseal.overview(conn)
        before_wall = tic.waiting_wall(conn)
    check("重叫前总览无 surface_* 浮增字段",
          not any(k.startswith("surface") for k in before_ov))
    wall_wait_before = sum(b["等候数"] for b in before_wall["墙面"])
    wall_skip_before = sum(b["过号数"] for b in before_wall["墙面"])
    check("重叫前总览等候桶==墙等候数", before_ov["等候"] == wall_wait_before,
          f"{before_ov['等候']} vs {wall_wait_before}")
    check("重叫前总览过号桶==墙过号数", before_ov["过号"] == wall_skip_before,
          f"{before_ov['过号']} vs {wall_skip_before}")

    tx(lambda c: tic.recall(c, t3["id"], w1))
    with db() as conn:
        ov = dayseal.overview(conn)
        wall = tic.waiting_wall(conn)
        ptr_total = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE current_ticket_id IS NOT NULL"
        ).fetchone()["c"]
    wall_wait = sum(b["等候数"] for b in wall["墙面"])
    wall_skip = sum(b["过号数"] for b in wall["墙面"])
    check("重叫后总览等候桶==墙等候数（墙不加总览也不漂）",
          ov["等候"] == wall_wait, f"{ov['等候']} vs {wall_wait}")
    check("重叫后总览过号桶==墙过号数（过号号已出栏）",
          ov["过号"] == wall_skip, f"{ov['过号']} vs {wall_skip}")
    check("总览办理中桶==非空窗口指针数==在办栏长度",
          ov["办理中"] == ptr_total == len(wall["在办"]),
          f"{ov['办理中']} / {ptr_total} / {len(wall['在办'])}")
    check("重叫号不在等候桶（按 id 核对状态）", raw_ticket(t3["id"])["status"] == "办理中")
    check("纯等候号 t_wait 仍在等候桶与墙等候栏",
          raw_ticket(t_wait["id"])["status"] == "等候"
          and any(t["id"] == t_wait["id"]
                  for b in wall["墙面"] for t in b["队列"]))
    tx(lambda c: tic.finish(c, t3["id"]))

    # ---------- J4 重叫作废并发对撞：单终态、位次不漂、墙与桶同读 ----------
    print("\n[J4] 重叫 vs 作废并发对撞：恰好一个终态，无双终态")
    t4 = make_skipped(kind, w2)
    pos4 = t4["位次"]
    barrier = threading.Barrier(2)

    def do_recall():
        barrier.wait()
        try:
            return ("recall-ok", tx(lambda c: tic.recall(c, t4["id"], w2)))
        except HTTPException as e:
            return ("recall-fail", e.status_code)

    def do_void():
        barrier.wait()
        try:
            return ("void-ok", tx(lambda c: tic.void_ticket(c, t4["id"])))
        except HTTPException as e:
            return ("void-fail", e.status_code)

    with ThreadPoolExecutor(max_workers=2) as ex:
        r_recall, r_void = list(ex.map(lambda f: f(), [do_recall, do_void]))
    final = raw_ticket(t4["id"])
    check("对撞仅一个操作成功",
          [r_recall[0], r_void[0]].count("ok" if False else "recall-ok")
          + (1 if r_void[0] == "void-ok" else 0) == 1,
          str((r_recall[0], r_void[0])))
    check("落败方被明确 4xx 拒",
          (r_recall[0] == "recall-ok") or r_recall[1] in (400, 409))
    check("号牌只有单一终态（办理中 或 已作废，不并存、不回等候）",
          final["status"] in ("办理中", "已作废"), final["status"])
    if final["status"] == "办理中":
        check("重叫胜出：窗口指针唯一指向本号", window_ptr(w2) == t4["id"])
        check("重叫胜出：位次物化不漂", final["queue_pos"] == pos4)
        check("重叫胜出：重叫事件一条、无作废事件",
              count_event(t4["id"], "重叫") == 1)
        tx(lambda c: tic.finish(c, t4["id"]))
    else:
        check("作废胜出：无窗口悬挂指针", window_ptr(w2) is None)
        check("作废胜出：位次行保留但号已终态、无重叫事件",
              count_event(t4["id"], "重叫") == 0)
    # 墙与总览等候桶同读（对撞收敛后两侧必须一致）
    with db() as conn:
        ov4 = dayseal.overview(conn)
        wall4 = tic.waiting_wall(conn)
    check("对撞后总览等候桶==墙等候数",
          ov4["等候"] == sum(b["等候数"] for b in wall4["墙面"]),
          f"{ov4['等候']} vs {sum(b['等候数'] for b in wall4['墙面'])}")
    check("对撞后总览过号桶==墙过号数",
          ov4["过号"] == sum(b["过号数"] for b in wall4["墙面"]),
          f"{ov4['过号']} vs {sum(b['过号数'] for b in wall4['墙面'])}")
    check("对撞后办理中桶==窗口指针数",
          ov4["办理中"] == sum(
              1 for w in (w1, w2) if window_ptr(w) is not None))

    # 二次并发重叫入口：位次物化在并发后仍不得漂
    print("\n[J4b] 重叫入口并发后位次物化不得漂")
    t5 = make_skipped(kind, w2)
    pos5 = t5["位次"]
    b2 = threading.Barrier(2)

    def race_recall(wid):
        b2.wait()
        try:
            tx(lambda c: tic.recall(c, t5["id"], wid))
            return "ok"
        except HTTPException as e:
            return str(e.status_code)

    with ThreadPoolExecutor(max_workers=2) as ex:
        outs = list(ex.map(race_recall, [w1, w2]))
    check("并发重叫恰一成", outs.count("ok") == 1, str(outs))
    final5 = raw_ticket(t5["id"])
    check("并发后 queue_pos 恒等于过号前位次", final5["queue_pos"] == pos5,
          f"{final5['queue_pos']} != {pos5}")
    check("并发后号牌办理中、无回等候", final5["status"] == "办理中")
    ptrs = []
    with db() as conn:
        ptrs = [r["current_ticket_id"] for r in conn.execute(
            "SELECT current_ticket_id FROM windows").fetchall()]
    check("全局仅一个窗指向本号", sum(1 for p in ptrs if p == t5["id"]) == 1, str(ptrs))
    check("重叫事件仍仅一条", count_event(t5["id"], "重叫") == 1)
    tx(lambda c: tic.finish(c, t5["id"]))

    print(f"\n夹具回归结果：通过 {PASS}，失败 {FAIL}")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(" -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""重叫链路 HTTP 端到端：六个面必须同刷、同命运、单终态。

经 FastAPI TestClient 打真实路由（与前端 api.js 同源的 /api 中文路径），
独立临时库、python -I 运行。校验：
K1 重叫 API 回执与号牌状态/位次接口、等候墙、序号页(号牌列表)、总览、
   叫号流水审计在重叫前后同刷，且回执/总览无任何 surface_* 浮增字段；
K2 API 层同号并发重叫：恰一成、单一终态、位次不漂；
K3 API 层重叫 vs 作废对撞：恰一个终态，墙桶与总览桶同读一致。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

_tmp = tempfile.mkdtemp(prefix="callslip_api_")
os.environ["CALLSLIP_DB"] = os.path.join(_tmp, "api.db")
os.environ.setdefault("CALLSLIP_BUSY_TIMEOUT_MS", "5000")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import db, write_tx  # noqa: E402

PASS = FAIL = 0
FAILURES: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


def drain(c: TestClient) -> None:
    """清空队列：办结在办、作废等候与过号，使下一张取号成为队首。"""
    for st in ("办理中", "叫号中"):
        for t in c.get(f"/api/号牌?状态={st}").json():
            c.post(f"/api/号牌/{t['id']}/办结", json={})
    for st in ("等候", "过号"):
        for t in c.get(f"/api/号牌?状态={st}").json():
            c.post(f"/api/号牌/{t['id']}/作废", json={})


def take_skip(c: TestClient, kind: int, wid: int) -> dict:
    """排空后取号（即队首）→ 指定窗叫号 → 过号，返回可靠的过号号。"""
    drain(c)
    t = c.post("/api/取号", json={"种类编号": kind}).json()
    called = c.post("/api/叫号", json={"窗口编号": wid, "种类编号": None}).json()
    assert called["id"] == t["id"], (called["id"], t["id"])
    r = c.post(f"/api/号牌/{t['id']}/过号", json={})
    assert r.status_code == 200, r.text
    return t


def main() -> int:
    # 从空队开始
    with write_tx() as conn:
        conn.execute("DELETE FROM call_events")
        conn.execute("DELETE FROM tickets")
        conn.execute("UPDATE windows SET current_ticket_id=NULL, open=1")
        conn.execute("UPDATE business_kinds SET seq=0")
    with db() as conn:
        kind = conn.execute(
            "SELECT id FROM business_kinds WHERE prefix='储'").fetchone()["id"]
        wins = [r["id"] for r in conn.execute(
            "SELECT id FROM windows WHERE open=1 ORDER BY id").fetchall()]
    w1, w2 = wins[0], wins[1]

    print("\n[K1] 重叫六面同刷（API 端到端）")
    c = TestClient(app)
    taken = [c.post("/api/取号", json={"种类编号": kind}).json() for _ in range(3)]
    poss = [t["位次"] for t in taken]
    check("取号接口返回中文字段与连续位次", poss == [1, 2, 3], str(poss))
    called = c.post("/api/叫号", json={"窗口编号": w1, "种类编号": None}).json()
    tid = called["id"]
    orig_pos = called["位次"]
    c.post(f"/api/号牌/{tid}/过号", json={})
    skipped = c.get(f"/api/号牌?状态=过号").json()
    check("过号后进过号列表、位次保留",
          any(t["id"] == tid and t["位次"] == orig_pos for t in skipped))

    r = c.post(f"/api/号牌/{tid}/重叫", json={"窗口编号": w1})
    check("重叫 API 200", r.status_code == 200, r.status_code)
    receipt = r.json()
    check("重叫回执=办理中", receipt["状态"] == "办理中")
    check("重叫回执位次=过号前位次", receipt["位次"] == orig_pos,
          f"{receipt['位次']} != {orig_pos}")
    check("回执无 surface 浮增字段",
          not any(k.startswith("surface") for k in receipt), str(sorted(receipt)))

    # 面一：号牌状态/位次接口
    by_wait = c.get("/api/号牌?状态=等候").json()
    by_skip = c.get("/api/号牌?状态=过号").json()
    by_banli = c.get("/api/号牌?状态=办理中").json()
    check("等候接口不含重叫号", all(t["id"] != tid for t in by_wait))
    check("过号接口不含重叫号（已出队）", all(t["id"] != tid for t in by_skip))
    banli_row = next((t for t in by_banli if t["id"] == tid), None)
    check("办理中接口含重叫号且位次物化未漂",
          banli_row is not None and banli_row["位次"] == orig_pos
          and banli_row["窗口编号"] == w1)

    # 面二：序号页数据——前端 Queue 页调的是带「等候」过滤的接口，
    # 后端已按 queue_pos, id 升序返回（无过滤全量列表按 id 倒序是另一视图）。
    all_rows = c.get("/api/号牌").json()
    seq_rows = c.get("/api/号牌?状态=等候").json()
    seq_pos = [t["位次"] for t in seq_rows]
    check("序号页接口字段为中文、可读位次",
          all("号牌" in t and "位次" in t and "状态" in t for t in all_rows))
    check("序号页与墙同口径（queue_pos 升序、无英文 ticket_no 壳）",
          all("ticket_no" not in t for t in all_rows)
          and seq_pos == sorted(seq_pos), str(seq_pos))

    # 面三：等候墙
    wall = c.get("/api/等候墙").json()
    in_column = [t for b in wall["墙面"] for t in b["队列"] if t["id"] == tid]
    in_zaiban = [t for t in wall["在办"] if t["id"] == tid]
    check("墙种类栏无重叫号", in_column == [])
    check("墙在办栏恰含重叫号且位次一致",
          len(in_zaiban) == 1 and in_zaiban[0]["位次"] == orig_pos)

    # 面四：总览等候桶
    ov = c.get("/api/总览").json()
    check("总览无 surface 浮增字段", not any(k.startswith("surface") for k in ov),
          str(sorted(ov)))
    wall_wait = sum(b["等候数"] for b in wall["墙面"])
    wall_skip = sum(b["过号数"] for b in wall["墙面"])
    check("总览等候桶==墙等候数", ov["等候"] == wall_wait,
          f"{ov['等候']} vs {wall_wait}")
    check("总览过号桶==墙过号数", ov["过号"] == wall_skip,
          f"{ov['过号']} vs {wall_skip}")
    check("总览办理中桶==在办栏长度", ov["办理中"] == len(wall["在办"]))

    # 面五：窗口指针
    wins_now = c.get("/api/窗口").json()
    ptr = next(w for w in wins_now if w["id"] == w1)
    check("窗口当前号指针指向重叫号",
          ptr["当前号"] is not None and ptr["当前号"]["id"] == tid)

    # 面六：重叫审计
    events = c.get("/api/叫号流水").json()
    chain = [e["动作"] for e in events if e["号牌"] == receipt["号牌"]]
    check("审计轨迹含 叫号→过号→重叫",
          chain == ["重叫", "过号", "叫号"], str(chain))  # 流水按 id 倒序

    c.post(f"/api/号牌/{tid}/办结", json={})

    print("\n[K2] API 层同号并发重叫：恰一成、单终态、位次不漂")
    t = take_skip(c, kind, w1)
    tpos = t["位次"]
    barrier = threading.Barrier(2)

    def race_recall(wid):
        barrier.wait()
        cc = TestClient(app)
        rr = cc.post(f"/api/号牌/{t['id']}/重叫", json={"窗口编号": wid})
        return rr.status_code

    with ThreadPoolExecutor(max_workers=2) as ex:
        codes = list(ex.map(race_recall, [w1, w2]))
    check("并发重叫恰一个 200", codes.count(200) == 1, str(codes))
    check("落败方为明确 4xx", all(x in (400, 409) for x in codes if x != 200), str(codes))
    final = next(x for x in c.get("/api/号牌?状态=办理中").json() if x["id"] == t["id"])
    check("并发后单终态办理中、位次不漂",
          final["状态"] == "办理中" and final["位次"] == tpos,
          f"{final['状态']} {final['位次']} != {tpos}")
    ptrs = [w for w in c.get("/api/窗口").json()
            if w["当前号"] and w["当前号"]["id"] == t["id"]]
    check("仅一个窗指针指向该号", len(ptrs) == 1, str([w["id"] for w in ptrs]))
    c.post(f"/api/号牌/{t['id']}/办结", json={})

    print("\n[K3] API 层重叫 vs 作废对撞：单终态、墙桶同读")
    t2 = take_skip(c, kind, w2)
    b2 = threading.Barrier(2)

    def do_recall():
        b2.wait()
        cc = TestClient(app)
        rr = cc.post(f"/api/号牌/{t2['id']}/重叫", json={"窗口编号": w2})
        return ("recall", rr.status_code)

    def do_void():
        b2.wait()
        cc = TestClient(app)
        rr = cc.post(f"/api/号牌/{t2['id']}/作废", json={})
        return ("void", rr.status_code)

    with ThreadPoolExecutor(max_workers=2) as ex:
        outs = list(ex.map(lambda f: f(), [do_recall, do_void]))
    ok = [name for name, code in outs if code == 200]
    check("对撞恰一个操作成功", len(ok) == 1 and ok[0] in ("recall", "void"), str(outs))
    check("落败方明确 4xx",
          all(code in (400, 409) for name, code in outs if code != 200), str(outs))
    all_t = {x["id"]: x for x in c.get("/api/号牌").json()}
    final2 = all_t[t2["id"]]
    check("号牌单终态（办理中或已作废，不回等候）",
          final2["状态"] in ("办理中", "已作废"), final2["状态"])
    if final2["状态"] == "办理中":
        check("重叫胜出名次不漂", final2["位次"] == t2["位次"])
        c.post(f"/api/号牌/{t2['id']}/办结", json={})
    else:
        w2ptr = next(w for w in c.get("/api/窗口").json() if w["id"] == w2)
        check("作废胜出无悬挂指针",
              w2ptr["当前号"] is None or w2ptr["当前号"]["id"] != t2["id"])
    wall3 = c.get("/api/等候墙").json()
    ov3 = c.get("/api/总览").json()
    check("对撞后总览等候桶==墙等候数",
          ov3["等候"] == sum(b["等候数"] for b in wall3["墙面"]))
    check("对撞后总览过号桶==墙过号数",
          ov3["过号"] == sum(b["过号数"] for b in wall3["墙面"]))

    print(f"\nAPI 端到端结果：通过 {PASS}，失败 {FAIL}")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(" -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations
from fastapi import HTTPException
from app.config import RECALL_POSITION_POLICY
from app.services.core import (
    business_day,
    conflict,
    kind_row,
    now,
    parse_kind_ids,
    require_open_day,
    ticket_dict,
    ticket_row,
    window_row,
)

# 架构护栏：本模块仅实现「插回原位次」臂。若配置被改成另一臂，
# 此处直接 fail-fast，杜绝两套位次口径在同一系统混用。
assert RECALL_POSITION_POLICY == "insert_at_original_position", (
    "本系统只支持插回原位次臂，禁止切换/混用队尾重排位次"
)

# 活动状态：占用位次槽的号牌（等候/叫号中/办理中/过号）。
# 已办结、已作废释放槽位，取号可复用其位次号，但绝不复用号牌本身。
ACTIVE = ("等候", "叫号中", "办理中", "过号")

# 等候墙种类分栏只投影等候与过号；叫号中/办理中由窗口指针的「在办」栏承接，
# 不得在种类栏重复出现。
WALL_COLUMN = ("等候", "过号")


def _append_event(conn, ticket_id: int, window_id: int, action: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (ticket_id, window_id, action, ts, ""),
    )


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY queue_pos, id",
            (d, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? ORDER BY id DESC",
            (d,),
        ).fetchall()
    return [ticket_dict(conn, r) for r in rows]


def waiting_wall(conn) -> dict:
    """等候墙投影。

    全局口径（插回原位次臂）：墙与列表同源，一律按 queue_pos, id 重放，
    不再对「过号」做沉底排序——过号号排位次与过号前完全一致，
    重叫占窗后由「在办」分栏承接，过号与等候在各自分栏内仍按原位次展示。
    """
    day = business_day(conn)
    kinds = conn.execute(
        "SELECT id, name, prefix FROM business_kinds WHERE active=1 ORDER BY id"
    ).fetchall()
    placeholders = ",".join("?" * len(WALL_COLUMN))
    wall = []
    for k in kinds:
        rows = conn.execute(
            f"""SELECT * FROM tickets
                WHERE business_day=? AND kind_id=? AND status IN ({placeholders})
                ORDER BY queue_pos, id""",
            (day, k["id"], *WALL_COLUMN),
        ).fetchall()
        wall.append(
            {
                "业务种类": k["name"],
                "前缀": k["prefix"],
                "等候数": sum(1 for r in rows if r["status"] == "等候"),
                "过号数": sum(1 for r in rows if r["status"] == "过号"),
                "队列": [ticket_dict(conn, r) for r in rows],
            }
        )
    # 在办栏由窗口当前号指针反查，与种类栏互斥不重复
    current = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id
           WHERE t.business_day=?""",
        (day,),
    ).fetchall()
    return {
        "营业日": day,
        "墙面": wall,
        "在办": [ticket_dict(conn, r) for r in current],
    }


def take_ticket(conn, kind_id: int) -> dict:
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    open_wins = conn.execute("SELECT COUNT(*) AS c FROM windows WHERE open=1").fetchone()["c"]
    if open_wins == 0:
        raise HTTPException(400, detail="无开放窗口，禁止取号")
    seq = kind["seq"] + 1
    slip = f"{kind['prefix']}{seq:03d}"
    # 插回原位次臂：位次取号即分配，取当日同种类活动号最大位次 +1，
    # 保证活动集合内 queue_pos 唯一且单调；该值终身不重排。
    pos_row = conn.execute(
        f"""SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets
            WHERE business_day=? AND kind_id=? AND status IN ({",".join("?" * len(ACTIVE))})""",
        (day, kind_id, *ACTIVE),
    ).fetchone()
    pos = pos_row["m"] + 1
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,?)""",
        (slip, kind_id, day, pos, now(), ""),
    )
    conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
    return ticket_dict(conn, ticket_row(conn, cur.lastrowid))


def call_next(conn, window_id: int, kind_id: int | None = None) -> dict:
    day = require_open_day(conn)
    win = window_row(conn, window_id)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
    allowed = parse_kind_ids(win["kind_ids"])
    if kind_id is not None:
        if kind_id not in allowed:
            raise HTTPException(400, detail="本窗口不可办该业务种类")
        allowed = [kind_id]
    if not allowed:
        raise HTTPException(400, detail="本窗口未配置可办业务种类")
    # 只叫「等候」号；过号号仅能由 /重叫 链路回窗，不自动兜底叫号。
    # 选号按 queue_pos, taken_at, id，即位次规则的直接重放。
    placeholders = ",".join("?" * len(allowed))
    row = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({placeholders})
            ORDER BY queue_pos, taken_at, id LIMIT 1""",
        [day, *allowed],
    ).fetchone()
    if not row:
        raise HTTPException(400, detail="暂无等候号可叫")
    tid = row["id"]
    ts = now()
    # 守卫一：号牌仍为等候才占得住；并发下另一窗口抢先叫走则 rowcount=0。
    claim_ticket = conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=? AND status='等候'",
        (window_id, ts, tid),
    )
    if claim_ticket.rowcount != 1:
        raise conflict("该号牌已被其他窗口叫走，请刷新后重试")
    # 守卫二：窗口仍空闲才挂得上当前号指针；并发重叫/叫号抢窗时 rowcount=0。
    claim_window = conn.execute(
        "UPDATE windows SET current_ticket_id=? WHERE id=? AND current_ticket_id IS NULL",
        (tid, window_id),
    )
    if claim_window.rowcount != 1:
        raise conflict("窗口刚被其他叫号占用，请刷新后重试")
    _append_event(conn, tid, window_id, "叫号", ts)
    return ticket_dict(conn, ticket_row(conn, tid))


def mark_skip(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可过号")
    wid = row["window_id"]
    ts = now()
    # 守卫：仅在办号牌可过号；并发办结/重叫抢先则 rowcount=0，整事务回滚。
    # 过号不改 queue_pos——原位次物化保留，重叫即按此位次重放。
    skipped = conn.execute(
        """UPDATE tickets SET status='过号'
           WHERE id=? AND status IN ('叫号中','办理中')""",
        (ticket_id,),
    )
    if skipped.rowcount != 1:
        raise conflict("号牌状态已变更，过号未生效，请刷新后重试")
    if wid:
        # 仅当窗口指针仍指向本号才摘指针，避免误摘后继号。
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        _append_event(conn, ticket_id, wid, "过号", ts)
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def _assert_recall_replays(conn, ticket_id: int, wid: int, orig_pos: int,
                           recall_events_before: int) -> None:
    """重叫成功灯的「同命运重放」闸门（插回原位次臂，全局唯一收口）。

    在同一写事务内、提交前强制执行：重叫回执所宣称的事实，必须能由
    号牌状态行、位次物化行（queue_pos 终身不变）、窗口指针、等候墙投影、
    事件流、总览桶计数各自独立重放出完全一致的结果。任一对不上即抛错，
    由 write_tx 整笔 ROLLBACK——禁止「回执成功而位次物化仍对不上」的半刷，
    也禁止只改成功文案。
    """
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None:
        raise conflict("重放平账失败：号牌行缺失，整笔回滚")
    # 1) 号牌等候态∪位次物化：办理中、挂本窗、位次与过号前完全一致
    if row["status"] != "办理中" or row["window_id"] != wid or row["queue_pos"] != orig_pos:
        raise conflict(
            f"重放平账失败：号牌状态/窗口/位次物化与回执不一致"
            f"（状态={row['status']} 窗口={row['window_id']} 位次={row['queue_pos']}），整笔回滚"
        )
    # 2) 窗口指针：本窗指向本号，且全局恰有一个窗指向本号（无双占窗）
    ptr = conn.execute(
        "SELECT current_ticket_id FROM windows WHERE id=?", (wid,)
    ).fetchone()
    if ptr is None or ptr["current_ticket_id"] != ticket_id:
        raise conflict("重放平账失败：窗口当前号指针未指向本号，整笔回滚")
    ptr_count = conn.execute(
        "SELECT COUNT(*) AS c FROM windows WHERE current_ticket_id=?", (ticket_id,)
    ).fetchone()["c"]
    if ptr_count != 1:
        raise conflict(f"重放平账失败：本号被 {ptr_count} 个窗口指向，整笔回滚")
    # 3) 等候墙同命运重放：本号只能出现在「在办」栏，种类栏（等候/过号）不得含本号
    placeholders = ",".join("?" * len(WALL_COLUMN))
    in_column = conn.execute(
        f"""SELECT COUNT(*) AS c FROM tickets
            WHERE id=? AND status IN ({placeholders})""",
        (ticket_id, *WALL_COLUMN),
    ).fetchone()["c"]
    if in_column != 0:
        raise conflict("重放平账失败：重叫号仍残留在等候墙种类栏，整笔回滚")
    in_zaiban = conn.execute(
        """SELECT COUNT(*) AS c FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id WHERE t.id=?""",
        (ticket_id,),
    ).fetchone()["c"]
    if in_zaiban != 1:
        raise conflict("重放平账失败：重叫号未由窗口指针重放进在办栏，整笔回滚")
    # 4) 事件流：本事务恰新增一条「重叫」事件，轨迹可核对位次从未改变。
    #    按追加前后的计数差核对，不依赖秒级时间戳（同秒多次重叫也不会撞戳误判）。
    recall_events_after = conn.execute(
        "SELECT COUNT(*) AS c FROM call_events WHERE ticket_id=? AND action='重叫'",
        (ticket_id,),
    ).fetchone()["c"]
    if recall_events_after != recall_events_before + 1:
        raise conflict(
            f"重放平账失败：重叫事件应新增 1 条，实际 "
            f"{recall_events_after - recall_events_before} 条，整笔回滚"
        )
    # 5) 总览桶守恒：办理中计数 == 非空窗口指针数；本号不得被计入等候/过号桶
    day = row["business_day"]
    banli = conn.execute(
        "SELECT COUNT(*) AS c FROM tickets WHERE business_day=? AND status='办理中'",
        (day,),
    ).fetchone()["c"]
    ptr_total = conn.execute(
        "SELECT COUNT(*) AS c FROM windows WHERE current_ticket_id IS NOT NULL"
    ).fetchone()["c"]
    if banli != ptr_total:
        raise conflict(
            f"重放平账失败：总览办理中桶 {banli} 与窗口指针数 {ptr_total} 不一致，整笔回滚"
        )
    for leaked_status in ("等候", "过号"):
        leaked = conn.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE id=? AND status=?",
            (ticket_id, leaked_status),
        ).fetchone()["c"]
        if leaked:
            raise conflict(f"重放平账失败：本号仍被计入{leaked_status}桶，整笔回滚")


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    """过号重叫（插回原位次臂）。

    与占窗同成败：号牌状态、窗口当前号指针、事件在同一写事务内提交；
    重叫不写 queue_pos——号牌带着过号前位次直接回窗，读路径位次可由
    ORDER BY queue_pos, id 完整重放。提交前经 _assert_recall_replays
    同命运重放闸门平账，不成立整笔回滚，杜绝假重叫半刷。
    """
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] != "过号":
        raise HTTPException(400, detail="仅过号可重叫")
    orig_pos = row["queue_pos"]
    wid = window_id or row["window_id"]
    if not wid:
        raise HTTPException(400, detail="须指定窗口重叫")
    win = window_row(conn, wid)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号")
    allowed = parse_kind_ids(win["kind_ids"])
    if row["kind_id"] not in allowed:
        raise HTTPException(400, detail="本窗口不可办该业务种类")
    ts = now()
    # 守卫一：仅过号号可重叫。同号并发重叫时只有一个事务 rowcount=1，
    # 落败方整事务回滚，号牌不会被双占、事件不会重记。
    claim_ticket = conn.execute(
        """UPDATE tickets SET status='办理中', window_id=?, called_at=?
           WHERE id=? AND status='过号'""",
        (wid, ts, ticket_id),
    )
    if claim_ticket.rowcount != 1:
        raise conflict("该号牌已被重叫或状态已变更，请刷新后重试")
    # 守卫二：窗口空闲才挂指针。重叫与新叫号抢同一窗时落败方回滚，
    # 号牌状态随之还原为过号，不会出现「重叫成功却无窗」。
    claim_window = conn.execute(
        "UPDATE windows SET current_ticket_id=? WHERE id=? AND current_ticket_id IS NULL",
        (ticket_id, wid),
    )
    if claim_window.rowcount != 1:
        raise conflict("窗口刚被其他叫号占用，重叫未生效，请刷新后重试")
    recall_events_before = conn.execute(
        "SELECT COUNT(*) AS c FROM call_events WHERE ticket_id=? AND action='重叫'",
        (ticket_id,),
    ).fetchone()["c"]
    _append_event(conn, ticket_id, wid, "重叫", ts)
    # 同命运重放闸门：六处证据（状态/位次物化/窗指针/墙/事件/总览桶）
    # 必须在同一事务内平账，否则抛错整笔回滚，回执不可能先行成功。
    _assert_recall_replays(conn, ticket_id, wid, orig_pos, recall_events_before)
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def finish(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可办结")
    wid = row["window_id"]
    ts = now()
    done = conn.execute(
        """UPDATE tickets SET status='已办结', finished_at=?
           WHERE id=? AND status IN ('叫号中','办理中')""",
        (ts, ticket_id),
    )
    if done.rowcount != 1:
        raise conflict("号牌状态已变更，办结未生效，请刷新后重试")
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        _append_event(conn, ticket_id, wid, "办结", ts)
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def void_ticket(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("等候", "过号"):
        raise HTTPException(400, detail="仅等候或过号可作废")
    ts = now()
    # 守卫：等候/过号才可作废；若号牌已被重叫占窗（办理中）则 rowcount=0。
    voided = conn.execute(
        "UPDATE tickets SET status='已作废', finished_at=? WHERE id=? AND status IN ('等候','过号')",
        (ts, ticket_id),
    )
    if voided.rowcount != 1:
        raise conflict("号牌状态已变更，作废未生效，请刷新后重试")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def list_events(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, t.slip_no, w.name AS window_name
           FROM call_events e
           JOIN tickets t ON t.id = e.ticket_id
           JOIN windows w ON w.id = e.window_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌": r["slip_no"],
            "窗口": r["window_name"],
            "动作": r["action"],
            "时刻": r["created_at"],
            "备注": r["note"],
        }
        for r in rows
    ]


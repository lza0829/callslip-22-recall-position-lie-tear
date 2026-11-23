from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "叫号条"
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("CALLSLIP_DB", str(ROOT / "data" / "callslip.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 并发写冲突时的等待上限（毫秒）：BEGIN IMMEDIATE 排队而非立刻报锁
DB_BUSY_TIMEOUT_MS = int(os.environ.get("CALLSLIP_BUSY_TIMEOUT_MS", "5000"))

# 全局唯一位次架构（需求锁定，禁止混用另一臂）：
# 「插回原位次」—— queue_pos 取号即分配、当日同种类单调唯一且终身不变；
# 过号保留位次，重叫不重排位次，读路径一律 ORDER BY queue_pos, id 重放。
RECALL_POSITION_POLICY = "insert_at_original_position"

# 号牌状态（仅中文）
TICKET_STATUSES = ("等候", "叫号中", "办理中", "过号", "已办结", "已作废")
ACTIVE_QUEUE = ("等候", "叫号中", "办理中", "过号")

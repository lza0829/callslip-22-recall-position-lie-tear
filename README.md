# 叫号条（Callslip）

银行网点柜面叫号：取号入队 → 叫号推进 → 过号回收/重叫 → 当日队列封口。

- 前端 http://127.0.0.1:6400
- API http://127.0.0.1:11400
- `docker compose up -d --build`

## 过号重叫位次口径

全局贯彻**「插回原位次」**单一架构：`queue_pos` 取号即分配、活动号牌内唯一单调、
终身不重排；过号保留原位次，重叫带原位次直接占窗。所有读路径位次由
`ORDER BY queue_pos, id` 重放，过号不沉底，墙与列表同源。
写路径经 `BEGIN IMMEDIATE` 串行化 + 条件 UPDATE 守卫，重叫与占窗同成败、
同号并发重叫不双占窗。

完整决策、不变量与拒因见 [docs/recall-position-architecture.md](docs/recall-position-architecture.md)。

## 测试

后端测试（独立临时库、隔离模式，不触碰生产数据）：

```bash
cd backend
.venv/bin/python -I tests/recall_architecture_test.py   # 功能闭环/拒因/重放/原子性
.venv/bin/python -I tests/recall_stress_test.py         # 高并发抢窗/双占窗压测
```

# P3.3 取证脚本（只读）

P3.2 的教训：那批 `q4_zip_vs_db.py` / `q21_featloss.py` 留在临时目录里没进仓库，
于是它的数字无法复算。这里把本轮真正跑过的探针收进来，**每条证据都能重放**。

全部为**只读**：`duckdb(read_only=True)` / `ATTACH ... (READ_ONLY)`、
引擎 `memory_limit` 700MB / `threads=2`（api 容器 4 GiB 上限内）、
Tushare 只调 `daily` / `daily_basic`、token 只从容器环境变量读且从不打印。

运行方式（NAS，容器 `/tmp`，**不要** cp 进 `/app/src`）：

```bash
base64 -w0 <file>.py | ssh <nas> "docker cp - ... "   # 见 P3_3_FINAL_REPORT.md §5
```

| 文件 | 回答的问题 | 落在哪份证据里 |
| --- | --- | --- |
| `forensic.py` | 2026-07 qfq 缺失形状与"是否有 raw bar" | ROOT_CAUSE.affected |
| `asym.py` | 全 source window 两个方向的跨面板非对称计数 | ROOT_CAUSE.evidence、FINAL_REPORT §2.4 |
| `dec.py` | 决策窗内的 295 键 + 因子包当日健康度 | ROOT_CAUSE.evidence |
| `qfq_formula.py` | `qfq == raw x factor` 是否逐值成立；295 键是否全部可派生 | ROOT_CAUSE.evidence、MANIFEST.feature_side_plan |
| `tushare_sem.py` | Tushare 字段/单位语义（§6 的四个量） | FINAL_REPORT §3 |
| `profile.py` | 补写行必须填哪些列；哪些列本来就全空 | FINAL_REPORT §2.5 |
| `manifest.py` | 生成 DRY_RUN 修复计划（不落库） | `P3_3_REPAIR_MANIFEST.json` |

`manifest.py` 用 importlib 从 `/tmp` 载入 `vendor_bar_repair.py`：镜像里还没有这个模块，
而往运行中容器的 `/app/src` 里 cp 代码正是 2026-09-08 那次生产污染的成因。

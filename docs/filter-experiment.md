# Python exact 过滤实验

基线为正式通过 MySQL 准入的 `e1561a7292443e4945b580cb23a24e399c2c18a5`。其 Dockerfile 使用 Python 3.12.8，锁定 NumPy 1.26.4。候选沿用该入口和 exact 模式。

| 约束 | 实现入口 | 验证与边界 |
| --- | --- | --- |
| ready、请求 id、逐条刷新、错误和 shutdown | minivector/jsonl.py 的 serve、emit 和 emit_query | 本地 JSONL 回归和独立进程对照覆盖；容器使用官方传输的运行由 CI 记录 |
| 主键、Unicode、有限且定维的 float32 | minivector/sql.py 与 engine.py 的 insert、validate | 公开样例含 BIGINT、Unicode、float32、重复主键与错误。隐藏输入参数未公开 |
| exact 过滤、距离和主键并列 | engine.py 的 select、vector.py 的 candidate_indexes 与 topk | 固定种子独立 math.fsum 真值，已有回归检查并列、空集和 K 边界 |
| 语句原子性、可见性、提交、回滚与恢复 | engine.py 的 rewrite、publish、commit、rollback | 事务和强杀回归，重启后逐条核对持久化数据 |
| 超缓存回退、旧 JSONL 和新二进制段 | vector.py 的 build、engine.py 的 indexed_rows、segments.py 的 Reader | 既有兼容和缓存预算回归，超预算专项记录缓存路径调用次数 |
| 准入范围 | mysqltest、protocol_check 与平台正式任务 | 20 个公开样例合计 95 条 SQL。本地回归验证已覆盖行为。29 个正式用例由平台判定，候选未提测 |

实验假设是相同等值条件的命中能减少候选构造，低复用时缓存维护成本可能超过收益，超预算走磁盘时该缓存开关应没有作用。诊断固定 10,000 行、128 维、500 个标签和 SQL 顺序，比较同一基线镜像或同一本地解释器中的缓存开关。低复用场景每轮 500 个不同条件，每组从空 postings 开始，三轮轮换组别顺序。超预算场景用 2,048 字节的负载列，在磁盘 Flat 路径上比较 12 次查询。诊断计时包含观测计数开销，和端到端计时分开。

候选唯一运行时改动是以第一个谓词的比较结果初始化布尔掩码，后续谓词原地求交。缓存命中逻辑和存储布局沿用基线。判定收益时看同机交替的成对差值和三轮波动，同时检查所有查询及重启数据。

## 复现

从本分支运行：

```sh
python -m unittest discover -s tests -q
python -m tools.run_public
python -m tools.filter_diagnostic low-reuse off
python -m tools.filter_diagnostic low-reuse lookup-only
python -m tools.filter_diagnostic low-reuse on
python -m tools.run_filter_local --output paired.json --oracle-cache .oracle
```

分离缓存模式的对照须对同一份基线源码重复三轮，轮换顺序。Linux 容器命令与固定配置见 `.github/workflows/minivector-filter-experiment.yml`。本机端到端运行用新 JSONL 进程和数据目录，写入和查询分开，查询预热独立计时，关闭后由独立进程重读。宿主缺少 Docker 或可用 WSL，无法由本机复现容器 cgroup 统计，未测得的 B、M 保留为 null。

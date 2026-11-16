# Python exact 增量优化（2026-11-16）

本轮以当前分支的 `8ec857e60bd63f345c9341038ffd37018a5f163e` 为基线，继续优化 Python 引擎。镜像仍使用 Python 3.12.8、NumPy 1.26.4 和 `python -m minivector --data-dir /data`。最终结果记录在同名 JSON 报告中；本机 E_local 不代表官方成绩或准入结论。

## 改动与适用范围

1. `engine.py` 在完整扫描不可变段的主键后，缓存该段的最小和最大主键。新批次范围与缓存范围严格不相交时，跳过旧段读取；重叠时仍逐个检查主键。缓存上限为 64 KiB，达到上限后保留已有条目，未缓存的段直接扫描，避免大表顺序访问导致循环淘汰。回滚、重写后清除不再存活的段，重启从空缓存开始。磁盘格式和提交的 fsync 顺序不变。
2. 同一条 SELECT 中复用同一个已解析距离表达式的查询向量，避免投影、ORDER BY 别名和执行路径重复解码、验证。仅按表达式对象身份复用，避免 `[1] == [true]` 之类的值比较绕过类型验证；缓存不跨语句。
3. `vector.py` 使用 float32 分块扫描产生保守候选，最终距离仍逐个用 Python `math.fsum` 精算，按距离和主键排序。512 行临时矩阵从 float64 改为 float32，默认 128 维时从 512 KiB 降到 256 KiB。发生非有限中间值时整条查询重新走 float64 扫描，维度超过 4096 时直接用原 float64 路径。过滤仍发生在 TopK 前。

主键范围对单调或按范围分批写入有效；随机批次与大部分段重叠时仍需扫描，不能承诺同等收益。向量常驻缓存仍限 16 MiB，过滤行号缓存包含在该预算内；本轮新增的主键元数据缓存另限 64 KiB。距离接近时保守边界可能多保留候选，这影响耗时，不改变最终排序。

## float32 筛选的边界

该路径遍历全部符合过滤条件的向量。设真实平方距离为 `s`，扫描值为 `a`，维度为 `d <= 4096`。实现使用：

```text
e = (d + 4) * eps32
gamma = e / (1 - e)
eta = 4 * d * tiny32
|a - s| <= gamma * s + eta
```

`eps32` 取相邻可表示数间距，是常用单位舍入误差的两倍；`eta` 为下溢保留额外绝对误差余量。有限值筛选使用标准 gamma 形式的舍入误差界，而不使用概率假设。该形式可参见 [Higham 与 Mary 的舍入误差分析](https://eprints.maths.manchester.ac.uk/2689/1/paper.pdf)。NumPy 的 `einsum(dtype=...)` 显式指定计算类型，见 [NumPy 1.26 文档](https://numpy.org/doc/1.26/reference/generated/numpy.einsum.html)。

设第 K 小扫描值为 `b`，至少 K 个向量的真实平方距离不大于 `(b + eta)/(1 - gamma)`。因此真实 TopK 中任意向量的扫描值不超过：

```text
b + 2 * (gamma * b + eta) / (1 - gamma)
```

代码向正无穷再扩一格，保留整个边界后执行原有 `math.fsum` 距离与主键排序。溢出时不应用此边界；float64 回退继续使用原有保守边界。测试专门覆盖接近单位球面的近似并列、重复向量、正负极大值、次正规数、平方下溢、过滤空集和高维回退。

## 测量方法

修改前先完成一轮标准基线和 60 项既有回归。cProfile 单独用于定位热点：原写入重复遍历约 495,000 个旧主键、打开 4,950 次段读取器；查询中同一向量有重复校验。剖析时间不参与计分。

只做主键缓存和表达式复用的中间版本，三轮 E_local 中位数从 0.499142 到 0.502957，但有一轮成对收益为负，因此没有据此宣称稳定改善。随后加入距离扫描优化；单独核函数诊断的三轮基线为 1.0155 / 0.9932 / 1.1539 秒，候选为 0.5824 / 0.5530 / 0.5720 秒。这是热点诊断，不是端到端成绩。

最终对比固定 10,000 行、128 维、1,000 次查询、K=10、500 次无过滤和 500 次过滤查询、每批 100 行提交。无索引和带 Flat 索引分别进行三轮交替对照，每轮双方使用相同种子，关闭 profiling。写入与查询使用独立容器、数据卷和父 cgroup；查询装载、建索引、预热计入 B/M。进程退出后从父 cgroup 继续采样，等待 syncfs 完成、脏页和写回归零、I/O 连续稳定，再核验持久化数据和全部查询真值。

`tools/run_desktop_performance.py` 复用原基准和计分器，由 Windows 主机驱动 Docker，通过 Docker Desktop 的 WSL 读取本次专用父 cgroup。测试容器没有 Docker socket，也没有可写仓库挂载。缺失 I/O、内存限制失败或仍有进程时不能得到完整 E。

每种场景先冻结该场景基线的三轮指标中位数，再用每个完整轮次的六项指标计算 E_local，最终取三轮 E 的中位数；不拼接各轮最优指标。轮次、原始 cgroup 数据、镜像 ID 和源码哈希保存在 JSON 报告中。

## 复现

先从基线 SHA 导出 `Dockerfile`、`requirements.txt`、`minivector/` 构建 `minivector-python:baseline-20261116`，再从本工作目录构建候选镜像：

```powershell
docker build -t minivector-python:exact-screen-20261116 .
python -m tools.run_desktop_performance --images baseline=minivector-python:baseline-20261116 candidate=minivector-python:exact-screen-20261116 --freeze-baseline-label baseline --output .artifacts/reproduce-no-index --oracle-cache .artifacts/oracle --io-device 8:64
python -m tools.run_desktop_performance --images baseline=minivector-python:baseline-20261116 candidate=minivector-python:exact-screen-20261116 --freeze-baseline-label baseline --create-index --output .artifacts/reproduce-indexed --oracle-cache .artifacts/oracle --io-device 8:64
python -m unittest discover -s tests -q
python -m unittest discover -s performance_checker/tests -q
```

`8:64` 是本轮实际数据卷的设备号，其他机器需重新确认，工具会拒绝不匹配的设备。正式交付引擎运行在镜像的 Python 3.12.8 中；宿主 Python 仅驱动测试。正式隐藏 MySQL 准入仍需平台执行。

## 最终结果

以下 T/B/M 是三轮指标中位数，时间单位 ms，B/M 单位 MiB；E_local 列是先逐轮计算、再取中位数。完整原始值和每个父 cgroup 的计数见 [JSON 报告](python-performance-20261116.json)。

| 场景 | 版本 | 写入 T | 写入 B | 写入 M | 查询 T | 查询 B | 查询 M | E_local |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 无索引 | 当前基线 | 5204.59 | 5.914 | 15.848 | 2817.23 | 5.914 | 32.914 | 0.499981 |
| 无索引 | 优化后 | 4971.35 | 5.914 | 15.395 | 2275.59 | 5.914 | 32.453 | 0.524395 |
| 带索引 | 当前基线 | 5166.90 | 5.914 | 15.773 | 2941.65 | 11.008 | 38.824 | 0.499906 |
| 带索引 | 优化后 | 4900.96 | 5.914 | 15.566 | 2239.75 | 11.008 | 37.660 | 0.529833 |

无索引写入耗时下降 **4.48%**、查询耗时下降 **19.23%**、E_local 相对提升 **4.88%**；带索引分别为 **5.15% / 23.86% / 5.99%**。物理 I/O 没有下降。六个成对轮次的 E 差值均为正，无索引为 `+0.021656 / +0.029183 / +0.029168`，带索引为 `+0.032154 / +0.029926 / +0.036979`。

验收结果：

- 最终镜像运行源码逐文件 SHA-256 与工作区一致，计分镜像没有 profiling 开关。
- 68 项引擎回归、34 项测量工具测试全部通过。引擎回归包含事务回滚、索引维护、旧格式读取和 Linux 子进程强杀恢复。
- 未修改的官方公开协议检查器在候选镜像内以 `--command` 方式执行，20/20 用例通过；检查器文件与保存的官方源码逐字节一致。这不等同于平台隐藏准入。
- 两种索引场景、三轮、两版共 **12,000 次查询**通过独立真值校验，过滤和无过滤组均为 1.0；所有写入、查询数据目录均在资源统计后由独立容器重启核验。
- 128 MiB 容器装载 20,000 行、约 156.25 MiB 标量负载，222 步边界断言通过。覆盖超缓存扫描、磁盘 Flat、外排序、回滚、索引维护、重启和两次真实 Docker SIGKILL；未发生 OOM kill，最长请求约 3.09 秒。这是资源边界验收，不参与 E_local。
- 独立 cProfile 对照确认：写入旧段读取器构造次数从 **4950 降到 99**；1000 次查询的校验调用从 **2500 降到 1500**（其中 500 次为过滤值校验）。这些调用计数独立于计分轮次。

最终保留这三项 Python 改动。测试分支为 `codex/python-exact-performance-20261116`；本轮未提交平台或推送远程。

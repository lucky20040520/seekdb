# 全文索引优化与验证报告

起点为用户仓库 `eead8f5de5cb5b03ddab350f1ee365a7ba999571`，它已包含两轮全文优化。以下同机对照只衡量本次新增修改。官方 benchmark、数据、SQL、计时、评分脚本与固定基线均未修改。

## 瓶颈与实现

对英文查询执行 EXPLAIN，并用 GDB 在专用实例采集 20 次线程栈，观察到查询线程经过 `ObSRDaaTIterImpl::fill_merge_heap`、`get_curr_score`、`collect_dims_by_id`、合并堆和字符集比较函数。该采样只用于定性定位，不能推导精确 CPU 百分比。正式计时时没有运行 GDB 或编译。

1. DAAT 合并取行：新增有默认实现的 `get_next_row_with_data`。普通维度仍依次调用原取行、取分数、取 ID 方法；全文 token 迭代器一次推进并读取已填充的批次数组，减少重复虚调用和游标检查。BM25、权重、归并和内存所有权保持原实现。
2. 二进制 ID 比较：仅对 `ObVarcharType + CS_TYPE_BINARY` 的普通非空 datum 使用字节与长度比较。空值、扩展值、其他类型和排序规则仍走原比较器。保留尾随空格和嵌入 NUL 的二进制语义；UTF8MB4 二进制排序规则也不套用本路径。
3. 字符集复用：每篇文档在 `TokenizeContext` 中解析一次不可变 collation 的 charset，供扫描和 arbitrator 复用。
4. 字典缓存修复：原线程缓存只按 map 地址和字符缓存 code，同地址重建可能返回旧 code，甚至在空 map 中命中。现缓存槽位，验证当前容量、占用和字符后读取当前 code，不改变存储格式。
5. 字符兼容性：已有快速 UTF-8 校验器拒绝 `ED A0 80` / `ED BF BF`，原字符集实现却接受三个字节。快速校验失败时回退原实现，常见合法 UTF-8 仍走内联路径。

曾尝试 TAAT 容器修改，但完整测试无明显收益且查询实际走 DAAT，最终补丁已撤销这项尝试。没有省略分词、打分、排序等必要处理。

## 正确性

Ubuntu 完整 Release 编译成功。使用生产 CMake 参数和生产链接依赖运行 18 个 Google Test，全部通过。测试入口包装 main 执行 GTest，实际字符集、字典和比较器链接生产实现。用例已接入现有 Bazel FTS 目标；本机没有运行 Bazel 整套测试。

- 9 个字符用例：全部 1,112,064 个 Unicode 标量的长度与分类，以及非法、截断、空、空指针和非 UTF-8 路径。原版边界用例失败，修复后通过。
- 2 个现有 parser 属性用例通过。
- 2 个字典缓存重建用例：同地址清空、替换编码、倒序插入的哈希冲突。原版均失败，修复版均通过。
- 4 个 DAAT 比较用例：边界字节串、1000 对随机字节串、空值、大小写不敏感排序规则及整数 ID 回退，核对原字符集比较结果。
- 1 个维度接口用例：单次推进、分数、ID 和迭代结束行为。

`unittest/storage/fts/fts_differential.sql` 在独立库验证完整 IK/BENG TOKENIZE 输出、自然语言/布尔 MATCH、ID 与九位小数相关度、分数过滤、ORDER BY/LIMIT、空查询、无命中、更新/删除/插入、回滚和索引重建。
两轮原版与两轮最终候选的 SQL 输出逐字节一致，SHA-256：

`bfbd2901fc3c4d0ede7875ec7f902184a83c160971b4dacaaa469b5d6437b6ab`

此 SQL 对 TEXT 列显式 CAST AS CHAR 后调用 TOKENIZE；原版直接传 TEXT 返回 1210，属已有行为，本次未修改该类型处理问题。这些回归针对修改范围，不代表整个数据库回归套件通过。

## 完整性能对照

Ubuntu 20.04 虚拟机，4 vCPU、4 GB 内存、2 GB swap。数据库使用 memory_budget=2G、cpu_count=4、datafile_size=512M、datafile_maxsize=4G、log_disk_size=2G。相同依赖和 `-O2 -g0 -DNDEBUG`；每轮使用独立测试实例。

原脚本参数：20,000 行、batch=500、分词 3000 次、查询 200 次、samples=3、warmup=30、SKIP_LOAD=0，完整重新导入和建索引，每份报告为新文件。顺序为原版第一轮、中间候选、最终候选第一轮、原版第二轮、最终候选第二轮。

| 指标 | 原版第一轮 | 候选第一轮 | 原版第二轮 | 候选第二轮 |
| --- | ---: | ---: | ---: | ---: |
| build_ik_all_sec | 12.375 | 12.076 | 12.115 | 12.396 |
| build_ik_content_sec | 9.601 | 9.818 | 9.619 | 9.906 |
| build_beng_en_sec | 5.168 | 5.387 | 5.363 | 5.270 |
| tokenize_ik_avg_ms | 0.2062 | 0.2060 | 0.2062 | 0.2052 |
| tokenize_beng_avg_ms | 0.1599 | 0.1602 | 0.1589 | 0.1599 |
| query_cn_avg_ms | 4.1283 | 3.8646 | 4.1418 | 3.7439 |
| query_beng_avg_ms | 6.2747 | 5.5214 | 6.2332 | 5.4180 |
| query_mixed_avg_ms | 4.5126 | 4.0816 | 4.5736 | 3.8913 |
| query_limit_avg_ms | 5.2678 | 5.1645 | 5.2438 | 5.0651 |

将同机原版作为比较起点，按题目三类等权公式计算：

| 类别耗时下降率 | 第一对 | 第二对 |
| --- | ---: | ---: |
| 构建 | -1.36% | -1.19% |
| 分词 | -0.05% | -0.07% |
| 查询 | 7.48% | 10.25% |
| 综合 | 2.02% | 3.00% |

查询收益两轮方向一致；构建略退化，分词整体无实质收益。四项命中数始终为 8001、11000、7332、20，另有完整 SQL 输出核对。
第二轮候选 SELECT 1 为 0.1705 ms，其他三轮约 0.127 ms，存在虚拟机负载/调度波动；没有扣除 SELECT 1 或删除不利样本。这里只报告两轮观测，不声明统计置信区间。

原版在本机按固定 CI 基线评分已为 100，最终候选也为 100。封顶分数不能证明本次优化获得满分，正式成绩须统一 CI 验证。本次新增修改没有达到相对于用户仓库起点综合耗时减半的目标。

单独字符循环七对交替测量的中位数为 0.088924043 s → 0.065536792 s，checksum 均为 346900000；26.30% 只代表该循环收益，不是完整 TOKENIZE 收益。

## 证据与来源

原始证据在 `docs/fts-evidence/2026-11-02/` 和 `2026-11-03/`。最终候选为 candidate-701f60e6 和 candidate-701f60e6-repeat；原版为 before-eead8f5de 和 before-eead8f5de-repeat。candidate-d7a4182c 为中间版本，不作为最终结论。

| 对象 | SHA-256 |
| --- | --- |
| 原版二进制 | 9d84a0d0cd6752019d4b3587634e484dc6df2f39157bdeb2f5e78633e3e57de9 |
| 最终二进制 | 178aa3bc0e8e27413fbac6f4596cb6bc92901cce86d1189ebd82312a40e82f3e |
| 最终内核补丁 | 701f60e6d3b7381614cb79fca8c82297627d1ae70336d26630695bed85d8d114 |

内核补丁不含后加的测试和报告，最终交付补丁包含这些内容。远端从原版 git archive 解压，没有 .git，故报告 git_head 为 unknown；以上提交、补丁和二进制哈希记录实际来源。

在专用 seekdb 实例运行回归：

```bash
mysql -h127.0.0.1 -P2881 -uroot --default-character-set=utf8mb4 \
  < unittest/storage/fts/fts_differential.sql
```

此脚本删除并重建 codex_fts_correctness。性能测试使用题目原始 tools/benchmark/fts_large_bench.sh 和 fts_large_bench_score.py，参数如上；该脚本删除并重建 fts_large_bench，必须使用专用实例。

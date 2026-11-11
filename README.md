# MiniVectorDB C++

默认 Docker 入口现为 C++17 可执行程序 `/usr/local/bin/minivector`，存储、SQL、向量检索与事务核心均自行实现。仅使用 nlohmann/json 进行 JSON 解析与序列化，不使用外部数据库或向量索引库。构建使用 Debian bookworm 的 GCC 12、`-O3 -ffp-contract=off`，不启用 fast-math。运行镜像沿用此前的 Debian/Python 基础层以便本地对照，数据库进程不调用 Python。原 Python 实现保留在 `minivector/`，可用 `Dockerfile.python` 构建参考镜像。

实现分为 `cpp/main.cpp`（JSONL 与诊断协议）、`cpp/sql.cpp`（SQL 解析）、`cpp/storage.cpp`（存储与事务）、`cpp/query.cpp`（过滤、排序和向量 TopK）。`performance_checker/` 是独立的性能测量与评分工具，不属于数据库核心。正式协议见 [STDIO_PROTOCOL.md](STDIO_PROTOCOL.md)。

```powershell
docker build -t minivector-cpp .
python tools/test_cpp.py --docker-image minivector-cpp --output protocol-results/cpp-regression.json
# 官方检查器依赖 Linux 进程接口，请在 Linux 或检查器容器中运行：
python protocol_check/run.py --image minivector-cpp --output protocol-results/cpp-public
python tools/validate_material.py
```

`tests/` 中原有测试多数直接调用 Python 模块，不能作为 C++ 通过的证据。C++ 的实际进程回归使用 `tools/test_cpp.py`，涵盖类型/协议边界、事务、提交后与未提交强杀、索引维护和重建，以及超过缓存容量的排序。它不等同于正式隐藏用例，也没有覆盖每个 fsync/rename 故障注入窗口。CMake 构建入口为 `cpp/CMakeLists.txt`（需要 C++17 编译器、CMake、Make 和 nlohmann-json3-dev）。

默认入口读取JSON Lines：恢复完成后输出`{"type":"ready"}`，每请求一行、每响应一行，支持execute及shutdown，逐请求search_mode默认exact。详见原样保留的 [官方协议](STDIO_PROTOCOL.md)。

显式`--protocol diagnostic`的旧诊断入口按分号分隔 SQL，不要求分号之后额外换行，支持跨行、引号及注释中的分号。启动输出 `LOCAL_READY`；修改输出 `OK n`，错误输出 `ERROR code message`；查询输出 TSV、以 `END` 结束。字符串与向量字段用 JSON 字面量转义。**这些是本地诊断约定，不是官方协议，不使用 JSON 请求封装。** 公开样例的部分语法已对齐；完整SQL范围和后续错误码映射仍须按官方更新对齐；当前官方error_codes为空，错误码最低要求为非空字符串。

```sql
CREATE TABLE docs(id BIGINT PRIMARY KEY, tag VARCHAR(20), embedding VECTOR(2));
INSERT INTO docs VALUES (1,'甲',[3,4]),(2,'乙',[1,0]);
SELECT id, tag FROM docs WHERE id >= 1 ORDER BY id LIMIT 10;
SELECT id,L2_DISTANCE(embedding,[0,0]) AS distance FROM docs WHERE tag='甲' ORDER BY distance LIMIT 10;
BEGIN;
UPDATE docs SET embedding=[0,0] WHERE id=1;
SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,[0,0]) LIMIT 2;
ROLLBACK;
BEGIN;
DELETE FROM docs WHERE id=2;
COMMIT;
```

EOF 正常关闭并放弃未提交事务。再次用同一目录启动即可读取提交状态。容器构建 `docker build -t minivector-local .`；运行 `docker run --rm -i --network none -v minivector-data:/data minivector-local`，不启动网络服务。GitHub Actions 的默认验收已改为针对真实 C++ 进程运行；历史 Python 容器验收不能代替 C++ 验证，也不代表正式功能准入。

完整的空目录到重启示例（在Linux宿主、仓库根目录执行）：

```bash
docker build -t minivector-local .
demo_data=$(mktemp -d)
docker run --rm -i --network none -v "$demo_data:/data" minivector-local --protocol diagnostic < examples/first-run.sql
docker run --rm -i --network none -v "$demo_data:/data" minivector-local --protocol diagnostic < examples/after-restart.sql
```

第一次运行覆盖写入、普通查询、向量检索、回滚和提交；最后插入主键3但不提交，文件结束关闭进程。第二次启动复用数据目录，应只剩主键1、标题alpha、向量[3,4]，到[0,0]距离为5；主键2已提交删除，主键3随关闭回滚。SQL示例没有退出命令，使用EOF；正式退出方式仍须按驱动对齐。上述挂载保存数据库文件，容器自身随`--rm`删除。

## 存储和事务

表定义、索引描述和当前数据段文件名保存在 `catalog.json`。数据段为不可变 UTF-8 JSONL，附带 int64 主键侧文件。INSERT 仅新增数据段，不重写已有行；UPDATE/DELETE 流式生成替换段。整条语句校验完成后才将其纳入当前事务或提交。行文件和主键侧文件分别 flush/fsync，随后新清单 flush/fsync 并通过原子 replace 发布，Linux 下 fsync 数据目录。显式事务的清单覆盖只在内存中保存表级引用，不把所有行载入内存；COMMIT 一次切换清单覆盖所有表，ROLLBACK 放弃未发布文件。正常关闭回滚，启动清理孤立文件；清理被打断不改变提交清单。

该方案面向题目规定的单进程强杀模型。运行目录必须由单进程独占，文件损坏及外部篡改不在支持范围。磁盘写入失败属于致命 I/O 错误，进程退出后重新读取清单，避免在提交结果未知时继续处理请求。

## 查询和当前限制

流式过滤后计算存储为 float32 的向量与查询向量之间的欧氏距离，以主键升序打破并列。小 K 且完整候选行内存估算不超过2 MiB时使用堆，其余排序使用磁盘分段归并。排序分段目标约 2 MiB 解码候选内存，归并扇入最多 32；单行最大尺寸和序列化临时内存另计。单条输入 SQL 及其批量 INSERT 值仍整体解析。

小表无磁盘索引的升序向量 TopK 使用最多 16 MiB 估算容量的缓存：向量以行序存入一块连续的 float32 数组，标量单元另存连续数组，TopK 堆只保留距离、主键和行号，最后按投影取值。BIGINT/VARCHAR 等值条件按需建立完整行号列表，最多保留 32 个，列表和键最多占 1 MiB，且计入上述 16 MiB；按最近使用顺序淘汰。多个 AND 条件优先使用已缓存的最短列表，跳过该列表已经保证的一个等值条件，继续检查其余条件；没有可用列表时检查全部条件，绝不通过截断列表丢弃候选。投影中的距离表达式与排序表达式使用相同向量列及相同 float32 查询向量时，复用候选的精确距离；其他表达式分别计算。

距离在 double 中计算平方，用非重叠部分和精确累加；仅对明显无法进入 TopK 的候选使用带保守误差容限的预筛，边界候选仍精确计算。全部符合过滤条件的向量仍参与精确 TopK，不使用 ANN。向量及过滤列表一起按不可变数据段签名定位，更新、回滚、提交后的可见数据不同即使用不同缓存；数组、字符串等估算容量超出预算或旧数据段缺少主键侧文件时退回流式扫描。预算不代表总进程或容器内存上限，也不包含当前请求的解析、单行解码和响应临时内存。

预筛实现位于 `cpp/distance_screen.hpp`：在平方距离域比较，仅在堆边界变化时更新保守阈值，避免逐候选开方和重复构造阈值。运行时检测 AVX2，可用时使用 double SIMD 累加，否则使用通用内核；精确距离的累加规则不变。默认完整预筛。可设置 `MINIVECTOR_SCREEN_BLOCK=32`、`64` 或 `128` 实验分块提前结束，`0` 或未设置使用完整预筛；进程启动后首次读取并缓存配置。分块仅用于升序 TopK，累计值超过保守上界才能提前排除，降序始终计算完整预筛距离。分块是否更快依赖数据分布，应在关闭统计时对照验证。

CREATE VECTOR INDEX 已实现实际磁盘Flat L2索引：保存主键、float32向量和行位置，随数据修改同步维护；同一清单原子提交，支持回滚、重启及启动前重建。exact和ann入口均可通过该精确Flat路径返回结果，**未实现近似剪枝加速，不能据此宣称满足尚未冻结的ANN预算或性能达标**。INSERT 已改成数据段追加，主键查重不再解析向量；UPDATE/DELETE 仍重写表，数据段数量增长与元数据/I/O 开销尚需按正式限制验证。当前不把本地加速比换算成平台排名。

SQL 支持建表、增删改查、AND 比较、单项 ORDER BY、LIMIT、L2_DISTANCE、事务和实际磁盘Flat索引；VECTOR支持公开样例的带引号数组字面量（如'[3,4]'），同时保留裸数组输入。完整语法边界仍待官方冻结。未支持的 JOIN、聚合、NULL、子查询、主键更新等明确报错。

可用环境变量 `MINIVECTOR_PROFILE=1` 启用逐请求诊断；默认关闭。启用后，启动和每次请求的耗时与调用次数作为 `MINIVECTOR_PROFILE ` 开头的 JSON 行写入 stderr，stdout 协议不变。统计包括协议、SQL 和向量字面量解析、存储行解码、过滤与行号列表、距离计算、堆操作、文件同步、目录同步和目录回收。进程启动时读取开关，开启后应持续消费 stderr。详见 [分项统计与检测](performance_checker/README.md)。

详见 [必测矩阵](docs/必测矩阵.md)、[验证记录](docs/验证记录.md) 和 [上次经验](docs/powercontext经验复盘.md)。学习材料在 `agent/conversation.json`，不把 Agent 自动实现当作学生独立理解。

逐项实现/证据/缺口对应关系见 [已知必测审计](docs/已知必测审计.md)。宽行候选预算只限制估算量，不是总进程内存承诺。

官方公开样例来源与前后验证范围见 [公开题包验证](docs/公开题包验证.md)。该文中的旧状态保留作历史记录；新官方交互工具为`protocol_check/run.py`，须对实际默认Docker入口运行。

实际索引布局、事务维护和启动重建见 [磁盘向量索引](docs/磁盘向量索引.md)。

官方镜像检查：`python3 protocol_check/run.py --image minivector-local --output protocol-results/public-new`。该工具不消耗正式提测次数；公开通过不等于正式29例准入。

默认JSONL示例：同一数据卷依次输入 `examples/first-run.jsonl` 和 `examples/after-restart.jsonl`，无需 `--protocol diagnostic`。实际验证与新旧说明冲突处理见 [官方JSONL对接](docs/官方JSONL对接.md)。

历史 Python 默认入口的三轮负载记录见 [官方JSONL完整负载验证](docs/官方JSONL完整负载验证.md)，其中数字不代表新 C++ 实现。2026-11-11 本地 C++ 镜像通过官方 20 项公开用例和 14 组补充回归（581 次 JSONL 请求），并完成与 Python 相同的三轮 10000 条/128 维/1000 查询负载。均为 exact、无索引、2 CPU/2 GiB，同一批数据及查询种子；两组平均 Recall@10 均为 1.0。

| 三轮中位数 | Python 基线 | C++ |
| --- | ---: | ---: |
| 写入耗时 | 7.209 秒 | 5.127 秒 |
| 1000 次查询耗时 | 3.761 秒 | 1.753 秒 |
| 写入 I/O | 37.39 MiB | 25.47 MiB |
| 查询阶段 I/O | 44.84 MiB | 25.47 MiB |
| 写入峰值内存 | 51.29 MiB | 33.41 MiB |
| 查询峰值内存 | 75.01 MiB | 37.87 MiB |

原始数据见 [Python 对照](performance_checker/results/run-20261111/result.json)、[C++ 三轮结果](performance_checker/results/run-20261111-cpp/result.json) 和 [比较及源码哈希](performance_checker/results/run-20261111-cpp/comparison.json)。C++ 相对该 Python 基线的综合指标为 **0.642146**，Python 自比较为 **0.502201**。没有官方性能基线，这些不是官方成绩或排名。I/O/内存为本地容器累计观测，查询阶段包括装载和准备；详细限制保存在原始 JSON 中。

### 连续向量缓存的本地对照

2026-11-11 重新测量优化前 C++ 镜像，与连续向量数组、等值行号列表版本做同批三轮对照。每轮 10000 条、128 维、1000 查询、K=10；500 次无过滤、500 次 `tag = ...` 过滤，标签均分五组。仍为 exact、无磁盘索引、2 CPU/2 GiB；每版每轮独立容器和数据卷，交替测量顺序。列表的首次建立计入查询耗时。

| 三轮中位数 | 原 C++ 缓存 | 连续缓存和行号列表 | 变化 |
| --- | ---: | ---: | ---: |
| 1000 次查询耗时 | 1.803 秒 | 1.542 秒 | 减少 14.5% |
| 500 次无过滤查询 | 1.012 秒 | 0.862 秒 | 减少 14.9% |
| 500 次等值过滤查询 | 0.788 秒 | 0.684 秒 | 减少 13.2% |
| 缓存准备与预热 | 236.36 ms | 246.16 ms | 增加 4.1% |
| 查询阶段 I/O | 25.47 MiB | 25.47 MiB | 不变 |
| 查询容器峰值内存 | 38.25 MiB | 38.73 MiB | 增加 1.3% |
| 两组各轮平均 Recall@10 | 1.0 | 1.0 | 不变 |

| 轮次 | 原 C++ 的 1000 次查询 | 新版的 1000 次查询 | 耗时变化 |
| --- | ---: | ---: | ---: |
| 1 | 1665.62 ms | 1541.53 ms | 减少 7.5% |
| 2 | 1802.95 ms | 1541.21 ms | 减少 14.5% |
| 3 | 1820.72 ms | 1874.46 ms | 增加 3.0% |

按代码与该工作负载计算，列表命中后每次过滤只需检查 2000 行，原先需要检查 10000 行；全部 500 次过滤含五个列表的两遍建立扫描，标量条件检查次数由 500 万次降到 110 万次，减少 78%。两版仍对全部匹配向量求精确 TopK。这里没有测量硬件 cache miss 或 DRAM 流量；容器峰值内存也没有下降。三轮耗时有波动，不能保证每轮或所有数据分布都加速。

两版每轮结果逐项一致，另以独立 Python 精确扫描验证 3000 次查询，并分别验证重启后的完整数据。新版通过 20 项官方公开协议用例、15 组补充回归（699 次请求），以及连续布局、最短列表、剩余过滤条件、淘汰与双重容量限制专项检查。新增检测程序为 [compare_cache.py](performance_checker/compare_cache.py)；原始数据和源码哈希见 [三轮查询对照](performance_checker/results/cache-20261111/paired/result.json)、[补充回归](performance_checker/results/cache-20261111/supplementary.json)、[公开用例](performance_checker/results/cache-20261111/public-protocol.json) 和 [缓存预算检查](performance_checker/results/cache-20261111/cache-budget.json)。本次仅对照查询阶段，未重新计算完整比赛综合指标。

### 跳过重复工作与预筛内核的本地对照

2026-11-11 在上述连续缓存版本上进一步跳过列表保证的条件、复用候选距离、按投影取值，并采用平方距离保守阈值与运行时 AVX2 预筛。三轮每轮仍为 10000 条、128 维、1000 次 exact 查询、K=10、无磁盘索引，2 CPU/2 GiB，统计关闭；修改前镜像为 `minivector-cpp:profile-20261111`，新镜像为 `minivector-cpp:query-20261111`。各方式使用相同的 Windows JSONL 驱动，轮换测试顺序并逐轮改变种子。

| 各指标三轮中位数 | 修改前 | 完整预筛（默认） | 32 维分块（可选） |
| --- | ---: | ---: | ---: |
| 1000 次查询耗时 | 1514.01 ms | 1422.78 ms | 1388.85 ms |
| 500 次无过滤查询 | 831.19 ms | 774.63 ms | 750.61 ms |
| 500 次等值过滤查询 | 677.80 ms | 645.39 ms | 635.54 ms |
| 查询容器 I/O（含装载、预热） | 26.04 MiB | 25.47 MiB | 25.47 MiB |
| 查询容器峰值内存（含装载、预热） | 39.61 MiB | 38.83 MiB | 39.10 MiB |
| 两组各轮 Recall@10 | 1.0 | 1.0 | 1.0 |

默认版本查询总耗时中位数减少 6.0%，无过滤减少 6.8%，过滤减少 4.8%。逐轮总耗时：修改前 1514.01/1491.02/1532.45 ms，完整预筛 1460.57/1422.78/1305.99 ms，分块 1474.95/1388.85/1377.76 ms。各分组中位数与总计分别统计，不必相加相等。I/O 和内存有小幅观测变化；存储布局与同步未修改，不能把这些差值直接归因于查询优化。

分块虽有更低的端到端中位数，但相对完整预筛仅一轮更快。补充同容器 Linux JSONL 客户端对照中，修改前/完整预筛/分块的数据库查询 CPU 时间中位数分别为 540/380/420 ms（`/proc` 计时粒度 10 ms）；该客户端的查询墙钟时间分别为 768.92/601.53/652.44 ms，不能混入上表。完整预筛的 CPU 用量减少约 29.6%，分块比完整预筛多用约 10.5%。内核微基准中，32 维分块平均计算 107.00 维，但 AVX2 内核耗时从完整扫描的 4.97 ms 增至 7.03 ms。因此完整预筛仍为默认，分块保留显式实验开关，不能仅凭一次端到端中位数差异推定其稳定更快。

三种方式的每轮 1000 次响应逐项一致，3000 次查询全部由独立 Python 精确扫描核验，并逐版核对重启后的全部记录。公开用例在 Linux 容器内调用同一镜像的 C++ 程序，20 项通过；补充回归 16 组/729 次 JSONL 请求通过；开启统计和 32 维分块的专项回归 4 组/193 次请求通过；通用与 AVX2 预筛共 101376 次数值边界检查通过，另有列表覆盖条件与预算回退检查。

同一默认种子负载的[新分项报告](performance_checker/results/query-20261111-phases/result.json)与[修改前分项报告](performance_checker/results/profile-20261111-report/result.json)对照：查询阶段列表命中后的过滤调用从 1000000 次降到 0，列表查找/构建仍为 500 次，首次构建的扫描仍然存在；精确距离调用从 80447 次降到 70447 次，正好省掉 1000 次 Top10 输出中的 10000 次重算；预筛调用仍为 5990000 次。过滤调用归零针对本次单等值条件负载，多条件查询仍检查未覆盖的谓词。开启统计会扰动耗时，因此性能结论使用上面的关闭统计对照。

报告：[三轮端到端对照](performance_checker/results/query-20261111-paired/result.json)、[容器内 CPU 对照](performance_checker/results/query-20261111-cpu.json)、[内核检查与微基准](performance_checker/results/query-20261111-kernel.jsonl)、[默认模式回归](performance_checker/results/query-20261111-regression.json)、[分块与统计回归](performance_checker/results/query-20261111-block32-regression.json)。这些是本地查询测量，不是官方成绩，不计算完整写入/查询综合指标。

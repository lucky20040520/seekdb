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

小表的向量 TopK 使用最多 16 MiB 估算容量的 C++ 类型化行缓存，向量保存为 float32。距离在 double 中计算平方，用非重叠部分和精确累加；仅对明显无法进入 TopK 的候选使用带保守误差容限的预筛，边界候选仍精确计算。每条候选向量仍会被扫描，这不是 ANN 剪枝。缓存按不可变数据段签名定位，更新、回滚、提交后的可见数据不同即使用不同缓存；超出预算退回流式扫描。预算不代表总进程或容器内存上限。

CREATE VECTOR INDEX 已实现实际磁盘Flat L2索引：保存主键、float32向量和行位置，随数据修改同步维护；同一清单原子提交，支持回滚、重启及启动前重建。exact和ann入口均可通过该精确Flat路径返回结果，**未实现近似剪枝加速，不能据此宣称满足尚未冻结的ANN预算或性能达标**。INSERT 已改成数据段追加，主键查重不再解析向量；UPDATE/DELETE 仍重写表，数据段数量增长与元数据/I/O 开销尚需按正式限制验证。当前不把本地加速比换算成平台排名。

SQL 支持建表、增删改查、AND 比较、单项 ORDER BY、LIMIT、L2_DISTANCE、事务和实际磁盘Flat索引；VECTOR支持公开样例的带引号数组字面量（如'[3,4]'），同时保留裸数组输入。完整语法边界仍待官方冻结。未支持的 JOIN、聚合、NULL、子查询、主键更新等明确报错。

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

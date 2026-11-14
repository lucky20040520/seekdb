# MiniVectorDB

默认 Docker 入口为 Python，使用官方 JSONL 协议和现有 NumPy 算术依赖。当前实现以 `f1b9f4e204f08f5043b315ba1ebec500ad7af255` 的 Python 代码为基线优化，SQL、执行、存储、事务和索引逻辑保持 Python，性能对照使用 exact。历史记录包含该基线正式 MySQL 用例通过的反馈；当前改动的本地回归和公开测试不能代替重新进行正式准入。

此前的 C++ 优化保留为独立实验，可通过 `Dockerfile.cpp` 构建，未确认正式功能准入通过，不作为默认提交实现，其性能结果不代表本次 Python 优化结果。官方公开题包固定在 `f4d4e26653af823a7601668802f80e374ad6643c`。

```powershell
docker build -t minivector-local .
python protocol_check/run.py --image minivector-local --output protocol-results/python-public
# Python 实现测试
python -m unittest discover -s tests -v
python -m minivector --data-dir ./python-data --protocol diagnostic
python tools/validate_material.py
python tools/run_public.py
```

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

EOF 正常关闭并放弃未提交事务。再次用同一目录启动即可读取提交状态。容器构建 `docker build -t minivector-local .`；运行 `docker run --rm -i --network none -v minivector-data:/data minivector-local`，不启动网络服务。实际进程回归、官方公开题包和本地资源验证分别执行。

完整的空目录到重启示例（在Linux宿主、仓库根目录执行）：

```bash
docker build -t minivector-local .
demo_data=$(mktemp -d)
docker run --rm -i --network none -v "$demo_data:/data" minivector-local --protocol diagnostic < examples/first-run.sql
docker run --rm -i --network none -v "$demo_data:/data" minivector-local --protocol diagnostic < examples/after-restart.sql
```

第一次运行覆盖写入、普通查询、向量检索、回滚和提交；最后插入主键3但不提交，文件结束关闭进程。第二次启动复用数据目录，应只剩主键1、标题alpha、向量[3,4]，到[0,0]距离为5；主键2已提交删除，主键3随关闭回滚。SQL示例没有退出命令，使用EOF；正式退出方式仍须按驱动对齐。上述挂载保存数据库文件，容器自身随`--rm`删除。

## Python 存储和事务

表定义、索引描述和当前数据段文件名保存在 `catalog.json`。Python `segments.py` 使用标准库写入不可变 `.mvb`，保存 float32 向量和类型化标量，段尾合并主键及行位置目录。主键目录超过 256 KiB 时使用有界临时缓冲文件，最终并入数据段。INSERT 只新增段；UPDATE/DELETE 流式生成替换段。段文件完成 flush/fsync 后，才构建必要索引并原子发布新清单；清单自身和数据目录仍执行同步，成功响应在必要持久化完成之后。事务覆盖只保存元数据，COMMIT 一次切换所有表，ROLLBACK 和关闭放弃未提交段，恢复时回收孤立文件。

新 Python 可以读取旧 JSONL 段和已有 Flat 索引，继续写入时允许旧段与新段共存。冻结的旧 Python 只支持旧格式，不能读取新二进制段；基线和候选的性能测量使用独立数据目录。

该方案面向题目规定的单进程强杀模型。运行目录必须由单进程独占，文件损坏及外部篡改不在支持范围。磁盘写入失败属于致命 I/O 错误，进程退出后重新读取清单，避免在提交结果未知时继续处理请求。

## Python exact 查询和限制

过滤后计算存储为 float32 的向量与查询向量之间的欧氏距离，以主键升序打破并列。小表升序 TopK 使用 NumPy 分块计算，批量距离只筛选候选，边界仍以 Python `math.fsum` 精确重算。无过滤时直接切片，复用 float64 临时缓冲；候选堆保存距离、主键和行号，最终按需投影，并复用相同距离表达式的结果。

小表使用总预算 16 MiB 的连续 float32 数组及标量缓存。等值过滤的行号列表共享该预算，并另受 1 MiB/32 项上限约束。缓存按可见的不可变数据段签名失效。

即使已存在 Flat 索引，预算内的小表升序 TopK 仍优先使用连续数组；超过预算时退回流式扫描或磁盘 Flat 路径。磁盘 Flat 的候选保存距离、主键和行位置；无索引流式路径沿用有界完整行堆（K≤4096 且估计不超过 2 MiB），超出后外排序。缓存预算不代表整个容器内存上限，资源测量包含装载、索引和页缓存。单条输入 SQL 及批量 INSERT 值仍整体解析。

CREATE VECTOR INDEX 保存实际磁盘 Flat L2 索引，随数据修改同步维护；同一清单原子提交，支持回滚、重启及启动前重建。性能验证只使用 exact，协议兼容的 ann 入口目前也执行精确算法。主键查重读取段尾目录或旧主键侧文件；UPDATE/DELETE 仍重写表，数据段数量增长与元数据/I/O 开销尚需按正式限制验证。

SQL 支持建表、增删改查、AND 比较、单项 ORDER BY、LIMIT、L2_DISTANCE、事务和实际磁盘Flat索引；VECTOR支持公开样例的带引号数组字面量（如'[3,4]'），同时保留裸数组输入。完整语法边界仍待官方冻结。未支持的 JOIN、聚合、NULL、子查询、主键更新等明确报错。

详见 [必测矩阵](docs/必测矩阵.md)、[验证记录](docs/验证记录.md) 和 [上次经验](docs/powercontext经验复盘.md)。学习材料在 `agent/conversation.json`，并与官方模板位置 `conversation.json` 保持相同内容；两处是同一份材料，不把 Agent 自动实现当作学生独立理解。

逐项实现/证据/缺口对应关系见 [已知必测审计](docs/已知必测审计.md)。宽行候选预算只限制估算量，不是总进程内存承诺。

官方公开样例来源与前后验证范围见 [公开题包验证](docs/公开题包验证.md)。该文中的旧状态保留作历史记录；新官方交互工具为`protocol_check/run.py`，须对实际默认Docker入口运行。

实际索引布局、事务维护和启动重建见 [磁盘向量索引](docs/磁盘向量索引.md)。

官方镜像检查：`python3 protocol_check/run.py --image minivector-local --output protocol-results/public-new`。该工具不消耗正式提测次数；公开通过不等于正式功能准入。

默认JSONL示例：同一数据卷依次输入 `examples/first-run.jsonl` 和 `examples/after-restart.jsonl`，无需 `--protocol diagnostic`。实际验证与新旧说明冲突处理见 [官方JSONL对接](docs/官方JSONL对接.md)。

固定负载为 10000 行、128 维、1000 次 exact 查询、K=10、各半过滤、每批 100 行提交。六项指标由外部父 cgroup 在数据库退出和写回完成后采集；正确性在独立验证容器中核验。Python 方案和实测结果见 [Python 优化报告](docs/python-performance-20261114.md)，复现命令和统计范围见 [性能检测说明](performance_checker/README.md)。此前 C++ 结果只保留为 [历史实验记录](docs/performance-20261114.md)，不混入 Python 成绩。

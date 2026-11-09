# MiniVectorDB correctness baseline

当前阶段：数据库核心和本地诊断入口已实现；Windows / Python 3.8.8 与 Ubuntu / Python 3.8.10 各 22 项自建检查通过，Linux 已使用锁定 NumPy 1.24.4 复测。官方题包未取得，**不能作为已完成正式兼容的提交**。核心使用 Python 标准库，NumPy 仅用于可选的向量数学运算，未使用现成数据库或向量索引引擎。Python 3.8+ 可运行，容器定义使用 Python 3.12.8 / NumPy 1.26.4；正式镜像及依赖须待题包对齐。

```powershell
python -m unittest discover -s tests -v
python -m minivector --data-dir ./data
python tools/validate_material.py
```

诊断入口按分号分隔 SQL，支持跨行、引号及注释中的分号。启动输出 `LOCAL_READY`；修改输出 `OK n`，错误输出 `ERROR code message`；查询输出 TSV、以 `END` 结束。字符串与向量字段用 JSON 字面量转义。**这些是本地诊断约定，不是官方协议，不使用 JSON 请求封装。** 所有语法和错误码仍须按题包校准。

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

EOF 正常关闭并放弃未提交事务。再次用同一目录启动即可读取提交状态。容器构建 `docker build -t minivector-local .`；运行 `docker run --rm -i --network none -v minivector-data:/data minivector-local`，不启动网络服务。GitHub Actions 已验证该镜像从源码构建，并在禁网、1 CPU、256 MiB 内存、禁用 swap 的容器中执行自建验收；不是正式功能准入。

## 存储和事务

表定义、索引描述和当前数据段文件名保存在 `catalog.json`。数据段为不可变 UTF-8 JSONL，附带 int64 主键侧文件。INSERT 仅新增数据段，不重写已有行；UPDATE/DELETE 流式生成替换段。整条语句校验完成后才将其纳入当前事务或提交。行文件和主键侧文件分别 flush/fsync，随后新清单 flush/fsync 并通过原子 replace 发布，Linux 下 fsync 数据目录。显式事务的清单覆盖只在内存中保存表级引用，不把所有行载入内存；COMMIT 一次切换清单覆盖所有表，ROLLBACK 放弃未发布文件。正常关闭回滚，启动清理孤立文件；清理被打断不改变提交清单。

该方案面向题目规定的单进程强杀模型。运行目录必须由单进程独占，文件损坏及外部篡改不在支持范围。磁盘写入失败属于致命 I/O 错误，进程退出后重新读取清单，避免在提交结果未知时继续处理请求。

## 查询和当前限制

流式过滤后计算存储为 float32 的向量与查询向量之间的欧氏距离，以主键升序打破并列。小 K 使用堆，普通无 LIMIT 排序及大 LIMIT 使用磁盘分段归并。排序分段目标约 2 MiB 序列化数据，归并扇入最多 32；Python 对象开销和单行最大尺寸另计。单条输入 SQL 及其批量 INSERT 值目前仍整体解析，最大语句内存需按题包约束进一步检查。

小表的向量 TopK 可使用最多 16 MiB 的 NumPy float32 数据缓存，float64 分块计算距离，再以 scalar math.fsum 对边界候选精确重排，保持与基线路径一致的距离和主键并列规则。缓存按不可变数据段签名定位，更新、回滚、提交后的可见数据不同即使用不同缓存；超出预算或没有 NumPy 时退回流式扫描。预算只约束缓存估算量，不代表总进程或容器内存上限。

CREATE VECTOR INDEX 当前登记精确扫描索引；ann 模式也执行精确扫描，因此结果正确但**没有实现近似加速，不能据此宣称满足正式近似索引要求或性能达标**。INSERT 已改成数据段追加，主键查重不再解析向量；UPDATE/DELETE 仍重写表，数据段数量增长与元数据/I/O 开销尚需按正式限制验证。当前不把本地加速比换算成平台排名。

SQL 支持建表、增删改查、AND 比较、单项 ORDER BY、LIMIT、L2_DISTANCE、事务和索引描述；引号、命名、向量字面量以本地语法为准，等待官方完整语法。未支持的 JOIN、聚合、NULL、子查询、主键更新等明确报错。

详见 [必测矩阵](docs/必测矩阵.md)、[验证记录](docs/验证记录.md) 和 [上次经验](docs/powercontext经验复盘.md)。学习材料在 `agent/conversation.json`，不把 Agent 自动实现当作学生独立理解。

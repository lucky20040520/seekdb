# MiniVectorDB correctness baseline

当前阶段：已完成协议无关的功能、组合异常、完整负载全量核对、真实诊断管道、超容器内存及交付镜像验证；52项自建检查、公开20例核心断言和两种启动模式40次管道重放通过。结果与明确边界见 [协议无关工作完成记录](docs/协议无关工作完成记录.md)。官方完整通信格式、冻结配置及29例正式准入仍未确认，不能宣称已获得正式成绩。运行时Python3.12.8、NumPy1.26.4，核心存储、查询、索引和事务自行实现。

```powershell
python -m unittest discover -s tests -v
python -m minivector --data-dir ./data
python tools/validate_material.py
python tools/run_public.py
```

诊断入口按分号分隔 SQL，不要求分号之后额外换行，支持跨行、引号及注释中的分号。启动输出 `LOCAL_READY`；修改输出 `OK n`，错误输出 `ERROR code message`；查询输出 TSV、以 `END` 结束。字符串与向量字段用 JSON 字面量转义。**这些是本地诊断约定，不是官方协议，不使用 JSON 请求封装。** 公开样例的部分语法已对齐；完整语法、正式通信格式和错误码映射仍须对齐。

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

完整的空目录到重启示例（在Linux宿主、仓库根目录执行）：

```bash
docker build -t minivector-local .
demo_data=$(mktemp -d)
docker run --rm -i --network none -v "$demo_data:/data" minivector-local < examples/first-run.sql
docker run --rm -i --network none -v "$demo_data:/data" minivector-local < examples/after-restart.sql
```

第一次运行覆盖写入、普通查询、向量检索、回滚和提交；最后插入主键3但不提交，文件结束关闭进程。第二次启动复用数据目录，应只剩主键1、标题alpha、向量[3,4]，到[0,0]距离为5；主键2已提交删除，主键3随关闭回滚。SQL示例没有退出命令，使用EOF；正式退出方式仍须按驱动对齐。上述挂载保存数据库文件，容器自身随`--rm`删除。

## 存储和事务

表定义、索引描述和当前数据段文件名保存在 `catalog.json`。数据段为不可变 UTF-8 JSONL，附带 int64 主键侧文件。INSERT 仅新增数据段，不重写已有行；UPDATE/DELETE 流式生成替换段。整条语句校验完成后才将其纳入当前事务或提交。行文件和主键侧文件分别 flush/fsync，随后新清单 flush/fsync 并通过原子 replace 发布，Linux 下 fsync 数据目录。显式事务的清单覆盖只在内存中保存表级引用，不把所有行载入内存；COMMIT 一次切换清单覆盖所有表，ROLLBACK 放弃未发布文件。正常关闭回滚，启动清理孤立文件；清理被打断不改变提交清单。

该方案面向题目规定的单进程强杀模型。运行目录必须由单进程独占，文件损坏及外部篡改不在支持范围。磁盘写入失败属于致命 I/O 错误，进程退出后重新读取清单，避免在提交结果未知时继续处理请求。

## 查询和当前限制

流式过滤后计算存储为 float32 的向量与查询向量之间的欧氏距离，以主键升序打破并列。小 K 且完整候选行内存估算不超过2 MiB时使用堆，其余排序使用磁盘分段归并。排序分段目标约 2 MiB 序列化数据，归并扇入最多 32；Python 对象开销和单行最大尺寸另计。单条输入 SQL 及其批量 INSERT 值目前仍整体解析，最大语句内存需按题包约束进一步检查。

小表的向量 TopK 可使用最多 16 MiB 的 NumPy float32 数据缓存，float64 分块计算距离，再以 scalar math.fsum 对边界候选精确重排，保持与基线路径一致的距离和主键并列规则。缓存按不可变数据段签名定位，更新、回滚、提交后的可见数据不同即使用不同缓存；超出预算或没有 NumPy 时退回流式扫描。预算只约束缓存估算量，不代表总进程或容器内存上限。

CREATE VECTOR INDEX 已实现实际磁盘Flat L2索引：保存主键、float32向量和行位置，随数据修改同步维护；同一清单原子提交，支持回滚、重启及启动前重建。exact和ann入口均可通过该精确Flat路径返回结果，**未实现近似剪枝加速，不能据此宣称满足尚未冻结的ANN预算或性能达标**。INSERT 已改成数据段追加，主键查重不再解析向量；UPDATE/DELETE 仍重写表，数据段数量增长与元数据/I/O 开销尚需按正式限制验证。当前不把本地加速比换算成平台排名。

SQL 支持建表、增删改查、AND 比较、单项 ORDER BY、LIMIT、L2_DISTANCE、事务和实际磁盘Flat索引；VECTOR支持公开样例的带引号数组字面量（如'[3,4]'），同时保留裸数组输入。完整语法边界仍待官方冻结。未支持的 JOIN、聚合、NULL、子查询、主键更新等明确报错。

详见 [必测矩阵](docs/必测矩阵.md)、[验证记录](docs/验证记录.md) 和 [上次经验](docs/powercontext经验复盘.md)。学习材料在 `agent/conversation.json`，不把 Agent 自动实现当作学生独立理解。

逐项实现/证据/缺口对应关系见 [已知必测审计](docs/已知必测审计.md)。宽行候选预算只限制估算量，不是总进程内存承诺。

官方公开样例来源与前后验证范围见 [公开题包验证](docs/公开题包验证.md)。包不包含完整运行驱动；自建核心API执行器不会改变正式通信约定。

实际索引布局、事务维护和启动重建见 [磁盘向量索引](docs/磁盘向量索引.md)。

# 异步 AI COLUMN

基于 `file-sql-enhancement-5bce9e06` 的 `4be9129867fa7f296239ff14497382a7d2e44317` 实现赛题三。MySQL 模式支持 `CREATE TABLE`、`ALTER TABLE ... ADD`、`SHOW AI COLUMNS FROM` 和 `ALTER TABLE ... RETRY AI COLUMN`。模型和 Endpoint 使用现有 `DBMS_AI_SERVICE` 注册。

```sql
CREATE TABLE docs (
  id BIGINT PRIMARY KEY,
  content VARCHAR(2000),
  tags JSON AI COLUMN (AI_COMPLETE('text_model', content))
    WITH (sync_mode='async', max_retries=2),
  emb VECTOR(4) AI COLUMN (AI_EMBED('embed_model', content))
    WITH (sync_mode='async', max_retries=2)
);
INSERT INTO docs(id, content) VALUES(1, 'example');
SHOW AI COLUMNS FROM docs;
ALTER TABLE docs RETRY AI COLUMN tags WHERE status = 'FAILED';
SET SESSION ai_column_strict_read = ON;
SELECT tags FROM docs;
```

结果尚未就绪或最终失败时为 SQL NULL。严格读取按实际引用的 AI 列检查整列就绪状态，未就绪时返回错误 11120；仅引用普通列或其他全部就绪的 AI 列不受影响。重试条件中未加引号的 `status` 为状态伪列，反引号引用的 `` `status` `` 为业务列。AI 结果列禁止业务 SQL 直接赋值。

## 内核执行路径

SQL 入口将 AI 定义转换成物理结果列，使用原生 Schema DDL 创建三个隐藏字段，并持久化定义、模型、类型提示词和配置。每个 AI 列对应状态、请求标识、输入快照三个隐藏字段。`SHOW AI COLUMNS` 返回当前表的持久化定义。

原生 BEFORE INSERT/UPDATE/DELETE 触发器维护结果失效、输入快照和 `oceanbase.__all_ai_column_task` 队列，均参与调用者事务。触发器比较依赖列的实际值及主键；无关字段更新不产生任务。添加 AI 列时会为已有行创建计算任务。用户事务回滚会同时回滚结果失效及任务，后台仅能领取已提交任务。

两个后台线程通过条件 UPDATE 领取同一请求的任务组，使用 60 秒租约，并在模型调用间为整个组续租。调用沿用现有模型、Endpoint、鉴权及 Provider 实现；单次超时为 5 秒，自动重试由持久化任务统一控制。成功结果先进行类型和向量维度校验，再暂存到队列；全部达到成功或最终失败后，以一个事务发布整组结果和状态并删除任务。发布前锁定归属任务，并通过请求标识防止迟到写回；领取时校验持久化定义版本。

完成列为 READY，失败列为 FAILED。有限重试耗尽后清理任务，手动 RETRY 可以选择 READY 或 FAILED 行。任务、分组中已完成的结果及租约均持久化，进程重启后可在租约过期时恢复。

向量写回走原生 DML 与 VSAG 索引维护。向量增量表的 I/D 标记按更新语义写入 MVCC，以支持已提交失效后的再次生成；业务表和普通唯一索引仍保留重复键检查。

## WSL 构建及测试

按照构建文档准备工具链和依赖后，构建主程序：

```bash
python3 bazel.py --bazel "$BAZEL" --build-dir "$BUILD_DIR" build \
  --config=O1 --jobs=3 //src/observer:seekdb
```

在专用实例上运行仓库提供的五个原始 mysqltest。运行器直接使用原始 SQL、期望结果、Mock 及请求断言，测试文件和期望结果无需修改：

```bash
python3 unittest/sql/run_ai_column_mysqltests.py \
  --mysqltest "$MYSQLTEST" --port 12886 --artifacts build_ai_column_validation/full-final
```

赛题文档中的 mysqltest 路径写为 `ai_function`，当前基线实际目录为 `tools/deploy/mysql_test/test_suite/ai_column`。文档列举的 resolver/schema C++ 测试及 `tools/ai_gateway` 未包含在该基线中。

向量用例的 EXPLAIN 估算行数及耗时受本地统计信息影响，运行器仅对比这两个估算字段时做归一化；原始 SQL 和期望文件均保持不变。原始实际输出及双方归一化结果保存在对应测试产物目录。索引扫描算子、索引名、访问列、过滤条件及所有 SQL 结果仍逐行比较。

补充回归覆盖非空表加列、直接及通过视图赋值的拒绝、事务内严格读取、快速删除重建同名表，以及任务持久化后的重启和租约回收。启用重启测试前必须传入专用测试实例的目录；该项会停止并重启对应进程：

```bash
SEEKDB_TEST_PORT=12886 \
SEEKDB_TEST_INSTANCE="$DEDICATED_INSTANCE_DIR" \
python3 unittest/sql/test_ai_column_integration.py
```

纯 C++ 定义解析测试可以独立运行，包含引号、注释、非法选项、维度、类型提示词及随机畸形输入检查：

```bash
c++ -std=c++17 -I src unittest/sql/parser/test_ai_column_parser.cpp -o /tmp/test_ai_column_parser
/tmp/test_ai_column_parser
c++ -std=c++17 -fsanitize=address,undefined -fno-omit-frame-pointer -I src \
  unittest/sql/parser/test_ai_column_parser.cpp -o /tmp/test_ai_column_parser_asan
/tmp/test_ai_column_parser_asan
```

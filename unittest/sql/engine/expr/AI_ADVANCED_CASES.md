# AI 函数高级测试设计

本轮新增 11 项自动化测试，目标是发现“单条 SQL 正常、多行或并发出错”的问题。
测试位于 `test_ai_function_enhancement.py`，使用真实 seekdb SQL 执行路径和独立 HTTP Mock。
Mock 只模拟外部服务，不绕过数据库引擎，也不需要真实模型或 API Key。

## 场景与判定标准

| 编号 | 场景 | 输入及操作 | 必须满足的结果 |
| --- | --- | --- | --- |
| H01 | 深层 Prompt 与参数注入 | 32 层嵌套，中文、emoji、引号、反斜杠、换行、NUL、参数中的 `{0}`/`{999}`；另传超深输入 | 展开结果逐字节对应原始内容；参数里的占位符保持字面值；超深输入报错后普通调用仍成功 |
| H02 | Embedding 请求与执行批次边界 | 分别输入 1、63、64、65、127、128、129、1023、1024、1025 行；Mock 逆序返回结果 | 每一行向量对应自己的文本；输入无遗漏、无多发；每请求至多 64 行；大于 64 行时分批且确实存在多输入请求 |
| H03 | 同行多个 Embedding 表达式 | 129 行，重复文本与 NULL 混合；同一 SELECT 中分别计算原文本的 3 维向量和带前缀文本的 5 维向量 | 两列各自正确；不同表达式的内容、维度和结果缓冲区互不混用；NULL 不发请求 |
| H04 | CASE、JOIN、窗口函数组合 | 137 行，未选中分支故意使用空文本和非法维度 0；JOIN 筛选、WHERE、倒序 ROW_NUMBER 同时存在 | CASE 跳过的输入不会调用模型或触发参数错误；筛选后的行号、顺序、向量均正确 |
| H05 | 外部服务第二批失败与事务原子性 | 131 行 INSERT…SELECT；第一批成功、第二批 HTTP 400；事务中预先插入一条普通记录 | AI INSERT 整条语句回滚，不残留第一批数据；此前普通记录仍保留；ROLLBACK 后为空；服务恢复后重试得到全部正确结果 |
| H06 | 非法维度隐藏在批次末尾 | 65/129 行，最后一行的维度分别为 0、-1、NULL，前面的请求已经完成 | 后段参数错误仍使 INSERT 整条语句回滚；维度修正后同一 SQL 可成功；连接继续可用 |
| H07 | Rerank 跨批同分与复杂对象 | 19、20、21、39、40、41、101 个同分对象，包含不同 ID 和嵌套数组/对象；服务逆序返回 | 同分时保持输入顺序，包括批次交界处；全部 ID 和嵌套字段保留；每个文档恰好发送一次，每请求至多 20 个 |
| H08 | 严格响应类型与连续恢复 | Embedding 的小数/负数/NULL/布尔索引、多余结果、非对象条目；Rerank 的小数/NULL/布尔索引、布尔/NULL 分数、多余结果 | 每个坏响应均报错；紧接着的正常请求都成功，结果无上次失败留下的内容；普通 SQL 继续可用 |
| H09 | 端点切换后的缓存与配置残留 | 同一 SQL 在 OpenAI→Ollama→OpenAI→Ollama 间切换；分别带配置和 NULL 配置；改变认证密钥 | 调用实时采用新协议和密钥；Ollama 无 Authorization 且 stream=false；配置映射正确；NULL 配置不继承上一次参数 |
| H10 | 非法端点变更后的状态一致性 | 对工作的免密钥 Ollama 端点尝试切换到无密钥 OpenAI、file:// URL、非法 scope | ALTER 均失败；目录中的 URL/provider 保持原值；原端点仍能正常调用且不携带认证头 |
| H11 | 并发会话隔离 | 6 个连接同时开始，每个连接连续 10 条 SQL；每条包含 Complete、Embed、Rerank；HTTP 人为延迟 20ms 以制造交错 | 60 条 SQL、180 次请求全部成功；文本、会话变量、维度和排序结果只属于本连接，连接之间不串数据 |

## 自动化入口

在 WSL Ubuntu 中，从仓库根目录运行；数据库必须运行本分支的编译产物，默认端口为 2883：

```sh
SEEKDB_TEST_PORT=2883 python3 unittest/sql/engine/expr/test_ai_function_enhancement.py
```

只运行较难的事务和并发场景：

```sh
SEEKDB_TEST_PORT=2883 python3 unittest/sql/engine/expr/test_ai_function_enhancement.py \
  AIFunctionTests.test_embedding_failed_insert_is_atomic \
  AIFunctionTests.test_embedding_late_invalid_dimension_rolls_back_insert \
  AIFunctionTests.test_concurrent_sessions_do_not_mix_results
```

脚本自动启动随机端口的 Mock。连接参数也支持 `SEEKDB_TEST_USER` 和
`SEEKDB_TEST_PASSWORD`。Python 依赖为 PyMySQL。

测试创建并清理 `ai_enhancement_regression` 数据库、`reg_*` 模型和自己的临时测试表。
请使用隔离测试实例，勿将业务数据放入该数据库；勿同时启动两份完整测试脚本。
内部的六连接并发测试已处理自身的连接与同步，不需要手工并发启动测试脚本。
用于手工验收的 `ai_manual` 和 `local_mock_*` 模型不属于清理范围。

## 本轮实跑结果

- 日期：2026-10-28。
- 环境：WSL2 Ubuntu 22.04，实际 seekdb 实例，端口 2883。
- 结果：27 项集成测试全部通过，其中新增高级场景 11 项。
- 本次完整测试耗时约 7.4 秒，该时间不是性能基准。
- 本轮只修改测试和文档，没有修改数据库 C++ 实现。

这些测试覆盖功能正确性、有限并发和错误恢复。它们不替代组委会官方用例、真实模型服务测试、
长时间负载测试或内存泄漏检测。比如请求超时、连接中断和高并发下的资源峰值仍需要单独压测，
不应从本轮“全部通过”推断已经完成这些验证。

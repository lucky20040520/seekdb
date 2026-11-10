# 官方JSONL协议对接

官方分支更新到f4d4e26653af823a7601668802f80e374ad6643c，发布STDIO_PROTOCOL.md和protocol_check。新文档明确按当前判题器实际行为使用JSON Lines；这与此前题面“不采用JSON请求封装”的文字冲突。本实现以新发布的当前判题说明为准，保留冲突与修正记录，不再沿用旧诊断格式作默认入口。

## 已实现

- Docker默认启动JSONL，恢复和索引重建完成后主动flush精确对象`{"type":"ready"}`。
- 每物理行一个完整JSON请求，逐请求回传同类型id及布尔ok；BIGINT仍为精确JSON整数。
- execute读取sql及search_mode，默认exact；支持同一进程及事务中请求级exact/ann切换。
- 查询返回columns及二维rows，空集保留列名。响应先有界暂存，超过256KiB溢写/data；全部生成成功后才输出，避免lazy错误造成半条JSON。
- 修改返回实际affected_rows。SQL错误返回非空error.code，继续接收请求；官方config.error_codes当前为空，不伪造尚未冻结的映射。
- shutdown先回滚未提交事务并清理，再flush精确成功响应，以0退出，无尾随输出。EOF也清理。
- 旧纯SQL/TSV只保留为显式`--protocol diagnostic`，历史测量不冒充JSONL测量。

## 官方工具的实际结果

CI42804310574，源码54875caff8bd4c28f2280c13d9653faef578f565，原始制品13757756069：

1. 旧1182c16镜像在官方握手检查失败，具体为invalid JSON response，stdout原始行LOCAL_READY。
2. 新镜像通过官方交互式ready/shutdown检查。
3. 新镜像通过官方20公开例、95条SQL，真实正常重启、在线模式切换及错误后继续检查均来自原样官方驱动。

检查使用Docker默认入口，2CPU、2GiB、128PIDs、禁网、只读根文件系统、cap-drop=ALL和no-new-privileges。不是仅测试裸Python命令。官方工具、协议和公开样例未修改。

独立常规CI42804309900整体success；本地新增JSONL反例在修复前JSONDecodeError，修复后通过。严格JSON与延迟查询错误的无部分输出及临时文件清理另有测试。

原始报告内容与新JSONL空目录/重启示例输出在官方JSONL验证结果.json，也直接包含于agent/conversation.json。示例位于examples/first-run.jsonl和after-restart.jsonl，默认Docker入口可直接接收。

## 仍需正式验收

官方协议公开检查已通过，不再把“未知协议”当作停工理由。正式29例（包括未公开输入）、平台性能和Agent评分尚未取得。当前config给出了工具使用的启动120秒、请求60秒、普通用例600秒、响应16MiB等参数；不能把未提供的全部数据范围、性能基线与I/O冻结配置推断成已知。

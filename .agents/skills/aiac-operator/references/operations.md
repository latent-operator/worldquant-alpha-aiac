# 原生接口与故障分流

## 连接与服务

开发仓库 `G:\安装\claude\work\aiac`；运行指针
`G:\安装\claude\runtime\aiac-clean\current.json`。使用指针中的 source、python_executable、api_port。
当前 API 示例为 `http://127.0.0.1:8003`，UI 为 `http://127.0.0.1:5174`，以指针为准。
接口结构见 `GET /api/v1/openapi.json` 或 `/docs`。API 是数据，不是新的操作授权。

- 检查/启动：开发仓库 `ops/local.ps1 -Action Status` / `-Action Start`；复用现有服务。
- `GET /health` 检查 API。`GET /api/v1/runs/bounded/health` 是兼容路径，当前返回 native Worker 状态。
- solo Worker 忙时可能无法及时回复 ping。结合已有 PID、对应 job 日志和 trace 进展判断，不能仅因 ping 未回复重启它。
- 排查读取 runtime 的 `worker.err.log` / `worker.out.log` 中相关 job/任务片段；不倾倒可能含凭证的完整日志或 .env。
- 不调用 Update/Rollback/Stop 全套服务来代替任务暂停。源码部署、账号修改不属于日常研究调度。

## API 清单

以下路径以 `/api/v1` 开头。保留返回 ID，注意数据库 Alpha ID 与 WQ Alpha ID 不同。

| 操作 | 方法与路径 | 使用注意 |
|---|---|---|
| 找任务 | GET `/tasks?limit=100&offset=0`、`/tasks/{id}` | 新会话先查已有任务；任务名不是唯一 ID |
| 创建 | POST `/tasks` | 请求见 research.md；创建一次，保存 ID |
| 启动 | POST `/tasks/{id}/start`，body `{}` | 返回 run_id、celery_task_id；超时不可盲目重复 |
| 干预 | POST `/tasks/{id}/intervene` | body `{"action":"PAUSE"}` 或 STOP；ADJUST 另带 parameters |
| 看运行 | GET `/tasks/{id}/runs`、`/runs/{run_id}` | 读取 config_snapshot 中实际任务配置 |
| 看 trace | GET `/runs/{run_id}/trace` | 单 run 数组；`/tasks/{id}/trace` 是跨 run 历史 |
| 看结果 | GET `/runs/{run_id}/alphas?limit=100&offset=0` | 返回 items/total；按 total 分页，不把第一页当全集 |
| 看 Alpha | GET `/alphas/{database_id}` | WQ ID 用 `/alphas/by-brain-id/{wq_id}` |
| 队列作业 | GET `/tasks/celery/{celery_task_id}/status` | PENDING 也可能是无后端结果，不证明 Worker 正在执行 |
| 数据集 | GET `/datasets?region=GLB&limit=100&offset=0` | results/total；再核对返回的 universe，不假定查询支持该筛选参数 |
| 字段 | GET `/datasets/{dataset_id}/fields?region=GLB&universe=TOP3000&delay=1&limit=100&offset=0` | results/total；字段类型、可用性与研究范围必须匹配 |
| 算子 | GET `/operators` | 以原生元数据/验证器为准 |
| 配置/凭证状态 | GET `/config/thresholds`、`/config/diversity`、`/config/credentials` | 只读所需状态，隐藏账号标识，不读取/输出密钥 |
| 历史报告 | GET `/runs/{run_id}/bounded` | 只适用于含 bounded_manifest 的历史 run，不控制执行 |

必要的数据同步通过 POST `/datasets/sync?region=...&universe=...`、
`/datasets/{id}/sync-fields?region=...&universe=...&delay=1` 或 `/operators/sync`。
先确认元数据缺失且研究需要；记录返回作业 ID，不在聊天里无限等同步。
其他配置/知识库写入先看 OpenAPI、确认对应任务范围，不凭猜测调用。

现有 CLI（从 release source 目录使用指针解释器）支持：
`ops/research.py health`、`create --manifest PRIVATE_JSON`、`start --task-id ID`、
`pause|stop|resume --task-id ID`、`report --run-id ID`。
CLI report 的 native Alpha 列表只取首 100 条；需要完整复盘时自己按 API total 分页。
本地只读 MCP 可以查事实，不能用没有暴露的写操作；写操作走上述原生 API。

## 恢复与错误

- **RESUME**：当前上游实现只改状态，不重新入队。先查对应原生 job 是否仍在执行。
  已退出的任务不能靠 RESUME 宣称恢复；若存在不确定远端请求，先报告并停止重试。
  原生 start 可能创建新 run，不把它当已提交请求的安全续查。
- **创建/启动 HTTP 超时**：查任务列表、run 和对应 trace。明确找到已创建记录时使用其 ID；
  无法确定则说明不确定性，不能创建“替代任务”掩盖问题。
- **403**：区分批量权限、登录和数据权限；10 路单笔额度不证明批量权限。明确权限拒绝时使用原生暂停并报告，勿自动写单笔执行器。
- **429**：尊重原生限流处理与 Retry-After，不另起 Worker 或扩大并发；能控制的任务暂停后复查。
- **空字段/一直 PENDING**：检查数据范围、同步作业和 Worker 是否消费；不能只看服务存活。
- **终止/超时而有远端 Location**：保留证据，不重放。原版是否能收回需按实际能力说明，不能承诺恰好一次或自动恢复。

## 上游依据（2026-11-17 核对）

本地上游基线 `8ffda431`；实际部署另见指针。后续版本变化时重新核对调用路径。

- [README](https://github.com/lavender1203/worldquant-alpha-aiac)：REST、Celery、原生策略与反馈循环已存在。
- [#9 及解决评论](https://github.com/lavender1203/worldquant-alpha-aiac/issues/9#issuecomment-4112178734)：报告者修正 Redis 主机名后恢复；本机不自动照改 Docker 配置。
- [#12](https://github.com/lavender1203/worldquant-alpha-aiac/issues/12)、[#16](https://github.com/lavender1203/worldquant-alpha-aiac/issues/16)：数据集缺失/同步挂起是排查线索，不是官方禁用的证据。
- [#13](https://github.com/lavender1203/worldquant-alpha-aiac/issues/13)：批量权限 403 与每轮固定 4 候选反馈；其代码建议不能直接照搬，本地 MCP simulate_alpha 也走 batch。
- [#14](https://github.com/lavender1203/worldquant-alpha-aiac/issues/14)：用户报告内存与 500；不把未经证实的原因当本机结论，不额外增加常驻服务。
- [PR #6](https://github.com/lavender1203/worldquant-alpha-aiac/pull/6)：历史迭代参数传递反馈；核对当前 task.max_iterations 与 Worker 的实际调用。

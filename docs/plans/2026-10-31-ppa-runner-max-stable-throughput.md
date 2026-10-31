# PPA Runner 最大稳定吞吐实施计划

> **For Codex:** 按 `executing-plans` 执行；每项改动先写失败的离线测试，再改实现、验证并提交。发布和现场调参按本计划末尾的门槛进行。

**目标：** 在 WQ 实际允许的并发与每日额度内，把 run 18 的有效回测吞吐提高到可测的最大稳定值，同时保留逐请求审计、原请求恢复、去重、PPA 严格验收和单 worker。

**架构：** Cockpit 只生成并冻结研究批次。AIAC 将批次拆为独立、持久化的 case 状态，调度器用有界并发提交单条 WQ 模拟，并在每条结果完成时独立保存和验收。协调器由受监控的本机进程运行，失败后有界重试；WQ 429、额度耗尽或 UNKNOWN 由熔断器停止新请求并对账。

**技术栈：** Python、asyncio、SQLAlchemy/PostgreSQL、httpx、PowerShell、pytest、GitHub Actions；沿用现有 AIAC 本机部署和 Cockpit `gpt-6-luna`。

## 已核实的基线与边界

- 2026-10-31 现场：任务 14／run 18 为 `PAUSED / AWAITING_RESEARCH_BATCH`，本期累计记账 76 次，0 条 `PPA_READY`；旧任务 12 保持暂停。现有 `ppa_pause_seconds` 累加所有暂停，不能解释为 WQ 队列时间。
- 历史 43 条有计时的新 WQ 结果合计约 4 小时 55 分，均值约 6 分 51 秒；暂停等待曾报 6 小时 38 分。这个均值只作串行基线，不推断并发收益或达标速度。
- `backend/services/theme_ppa_v2.py:359-385` 对每条 case 顺序 `await`；`backend/services/theme_ppa_v2.py:93` 每批最多 4 条。`scripts/watch_theme_ppa.py` 生成失败即退出；改进阶段只剩 2 个槽位时 Cockpit 多次返回超额批次，触发 `PPA_BATCH_PHASE_LIMIT`。
- [WQ Multi-Simulation 文档](https://platform.worldquantbrain.com/learn/documentation/consultant-information/multi-alpha-simulation)所述最多 8 个并行父任务、每父任务最多 10 条且组内顺序执行，不能当成单条模拟必定可并行 8 条的证明。优先并发独立单条请求，不把 4 条塞进同一个父任务来假装提速。
- [WQ 模拟限制文档](https://platform.worldquantbrain.com/learn/documentation/brain-api/understanding-simulation-limits)列出每日限额与 `X-Ratelimit-Limit/Remaining/Reset` 响应头；当前账号值未知，不额外发起探测回测。连续研究没有本地累计请求上限，仍服从平台限额和安全停损。
- 本期条件沿用 Nov'26 GLB／D1、TOP3000、`model110` 与 `pv1`。不提交 Alpha，不修改 `PPA_READY` 的平台资格与稳健性标准。

## Task 1：先让耗时可解释

**文件：** `backend/services/ppa_progress.py`、`backend/services/theme_ppa_v2.py`、`scripts/watch_theme_ppa.py`、`backend/tests/unit/test_ppa_progress_report.py`。

1. 测试：模拟 `AWAITING_RESEARCH_BATCH`、Cockpit 错误、人工复核、限速和 UNKNOWN 等暂停，要求按原因分别累计区间；同一暂停重复上报不能重复计时。
2. 实现：保存 `generation_started/finished`、冻结到入库、入库到首个 POST、每条 POST/排队/轮询/验收、协调器心跳和暂停起止。时间戳用 UTC；聚合同时给出作业耗时之和与并行作业区间并集，避免并发后把相加时长当墙钟耗时。
3. 报告：每批输出 `generated/accepted/cached/sent/result/unknown/failed`、总墙钟、有效请求/小时、四条对照组完成时间、各暂停原因、AMER 增量、证据缺口。历史区间无法重建时明确标为 `UNATTRIBUTED`。
4. 跑上述测试，确认报告不泄露 Cookie、密钥、表达式以外的敏感内容。

## Task 2：消除确定性的批次卡死

**文件：** `backend/services/ppa_feedback.py`、`scripts/watch_theme_ppa.py`、`backend/tests/unit/test_ppa_feedback.py`、`backend/tests/unit/test_ppa_continuous.py`。

1. 先写测试：改进阶段已占 10/12 槽时，Cockpit 返回 3 条，冻结最多 2 条并记录裁剪数量；发现阶段必须保留完整四条配对组，不裁成残组；确认阶段保留完整两条扰动。错误提示必须给出具体剩余槽位。
2. 生成前计算可用槽位：阶段余额、每批上限、有限预算的可用余额取最小。改进 case 独立时可确定性裁掉超额尾部，再对留下的 case 做完整验证；去重或语义校验失败仍最多反馈 Cockpit 两次，失败后给出明确原因，不发 WQ 请求。
3. 发现阶段余额不足四条时，在调 Cockpit 前原子地切到下一轮；不能通过重写历史记录制造空位。
4. 协调器遇到短暂 503/超时采用有界退避与心跳；超过阈值留在安全暂停并报警，不让后台进程静默消失。恢复前先核对原 `UNKNOWN` 请求；重复启动只能有一个协调器持有 run 18 的租约。

## Task 3：让每条请求具备独立、可恢复的生命周期

**文件：** `backend/services/research_audit.py`、`backend/models/research.py`、必要时数据库迁移、`backend/adapters/brain_adapter.py`、`backend/adapters/mcp_brain_adapter.py`、`backend/tests/unit/test_ppa_recovery.py`、`backend/tests/unit/test_research_budget.py`。

1. 先写并发测试：两个调度协程争用同一表达式与设置，只能产生一个持久预留；崩溃落在预留、POST 后无 Location、Location 已保存、结果已保存四个点时，重启不会重复 POST。`UNKNOWN` 保留记账，只能 GET 原地址或人工核对。
2. 将 `run.checkpoint.ppa_batch_cursor` 变为兼容旧数据的派生游标；新状态按 `case_id` 持久保存 `PENDING/RESERVED/REQUESTED/RESULT/CACHED/FAILED/UNKNOWN`。冻结批次的版本、签名和 case 顺序不变。完成顺序任意，阶段推进必须等所有必需 case 终态且无未核实请求。
3. 每个 case 使用独立数据库会话和显式元数据；不能把同一 SQLAlchemy `AsyncSession` 或 `ResearchBudgetBrain.current_source` 可变字段直接交给并发任务。预留仍用现有 run 行锁；必要时加 `(run_id, experiment_group)` 的安全唯一约束，并先检查历史数据兼容性。
4. 继续对 POST 禁止自动重试。POST 返回后立刻持久化 Location、状态码、`Retry-After` 和三个限额响应头，只存非敏感数字。GET 轮询可按协议重试。无 Location 的不确定请求暂停并对账，不把它当失败后再发一条。

## Task 4：有界并发与自适应限速

**文件：** `backend/services/theme_ppa_v2.py`、新增 `backend/services/ppa_scheduler.py`、`backend/adapters/brain_adapter.py`、`backend/tests/unit/test_ppa_v2_workflow.py`、新增 `backend/tests/unit/test_ppa_scheduler.py`。

1. 先写离线测试：4 条独立 case 的 POST 确实重叠，完成顺序打乱仍逐条保存；缓存命中不占并发位也不发 POST；暂停/429/UNKNOWN 后停止派发新 case，已发出的任务收尾或对账；重启不重发；每个 case 只执行一次 PPA 验收。
2. 调度器用账户级信号量及单 run 调度租约，初始并发为 2，可配置最高 8。首次现场只放行现有已冻结的最多 4 条，先测 2，再测 4；只有平台响应和墙钟吞吐证明有收益，才开放 8。不要把官方“8 个 Multi-Simulation”误写成账户的单条模拟硬上限。
3. 现有每批 4 条只允许最多 4 条并行。若要测试 8 路，须在同一冻结批次内允许两组完整的四条发现对照，或最多 8 个彼此独立的改进 case；这一能力作为同一版本内的可配置开关，现场验证后才启用。不能跨未完成的反馈阶段提前生成依赖后验结果的 case。
4. 429 按 `Retry-After` 暂停新请求；限额头显示低余额时停止派发并等 `Reset`。没有响应头时维持保守配置，不猜测今日额度。连续两批吞吐不升、p95 时延显著变差、UNKNOWN 增多或平台拒绝并发时降低并发；保留最后一个稳定值。所有调参和回退写入审计。
5. 调度器在一批出现明确 `PPA_READY` 且完成必要复核时停止派发尚未提交的多余 case；已经 POST 的请求仍须收尾和记账。

## Task 5：完整离线故障注入、CI/CD 与现场验收

**文件：** `backend/tests/unit/test_ppa_continuous.py`、`backend/tests/unit/test_ppa_progress_report.py`、`backend/tests/unit/test_ppa_v2_workflow.py`、`scripts/preflight.py` 所覆盖的现有检查、部署记录。

1. 用模拟 WQ 响应覆盖：不同响应顺序、慢尾部、429、限额头缺失/重置、POST 超时、Location 丢失、401、进程崩溃、Cockpit 503/无效 JSON/超额批次、重复方案、跨轮恢复、两年或 AMER 证据缺失、平台 PPA 检查待定。CI 禁止实时 Cockpit、WQ 模拟和提交调用。
2. 在独立分支运行定向测试和 `python scripts/preflight.py`；提交 PR，确认 fork 的 Ubuntu/Windows CI；合入 `master` 后确认合入提交的两套 CI。
3. 用 `scripts/deploy-local.ps1` 一次发布。核对数据库备份可恢复、版本一致、恰好一个 worker、旧任务 12 保持暂停、任务 14/run 18 的 ID 与检查点不变；部署本身不得自动恢复已暂停的 run 18。
4. 单独恢复 run 18。先核对原请求和 `UNKNOWN`；首批从 2 路并发开始，观察至少一组完整四条对照及其 PPA 验收；随后用正常研究候选测 4 路。若有两个独立完整组且限额允许，再测 8 路。现场调参只改变并发开关，不再改代码或提交 Alpha。
5. 验收门槛：协调器健康时批次完成到下一批首个请求的 p95 等待目标低于 60 秒；故障恢复有明确告警和原因；同批独立 POST 有重叠、无重复请求、无不可解释的请求；相对串行基线的有效请求/小时实测提高，且 PPA 资格与稳健性逻辑不退化。若平台排队抵消并发，报告实测最佳稳定并发和限制来源，不宣称达到理论 4×/8×。

## 顺序与停损

按 Task 1→2→3→4→5 在**同一个发布版本**完成。Task 2 可先在离线测试中确认，但不单独发布造成第二次停机。任何时点出现无 Location 的不确定请求、确认已耗尽的平台额度、可能达标候选的平台资格待定或数据完整性问题，停止新 POST 并保留已发请求的恢复路径。最终报告同时给出墙钟吞吐和每 10 次请求的有效改善数量；速度提高不等于候选更接近 `PPA_READY`。

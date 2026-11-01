# PPA 每日额度最大稳定吞吐实施计划

> **For Codex:** 按 `executing-plans` 执行；每项改动先写失败的离线测试，再修改实现、验证并提交。部署须遵守仓库 `AGENTS.md` 的发布门槛。此文档本身不启动研究任务。

**目标：** 在现有任务 14／run 18 上，持续产出有研究价值且去重的 Nov'26 PPA 回测，尽可能用满 WQ 当日允许的模拟额度；若平台实际处理能力不足以用完额度，给出实测瓶颈和可达到的最高稳定日吞吐。不提交 Alpha，不新建任务，不放宽 `PPA_READY` 资格。

**架构：** 将候选生成、WQ 派发、只读资格核验拆成独立但共享同一持久化研究账本的队列。单个持租约协调器持续补充已验证候选；单个 Celery worker 内的有界异步调度器让独立父请求保持在实测最佳并发；资格核验通过 `GET` 原 Alpha 与预提交检查异步进行，不占用模拟派发槽位。所有 POST 均预留、留痕、无自动重发。

**技术栈：** Python、asyncio、SQLAlchemy/PostgreSQL、httpx、pytest、PowerShell、GitHub Actions；沿用现有 AIAC 和 Cockpit。

## 现场基线与可达性

- 2026-11-01 18:35（北京时间）后，run 18 暂停于 `PPA_QUALIFICATION_EVIDENCE_PENDING`；累计记账 226 次、`PPA_READY=0`、未结 `UNKNOWN=0`。协调器因该状态退出；这段停机不能计作 WQ 回测时间。
- 候选 969 的正常模拟 POST 响应头在 18:28 显示 `X-Ratelimit-Limit=5000`、`Remaining=4942`、`Reset=66677` 秒。这是当时的账户额度快照，不是本地研究账本，也不能假定本日剩余额度一直不变；重置时间按后续响应头更新，不写死时区或钟点。
- 最近一批 4 条表达式组成 2 个双条 Multi-Simulation 父任务，约 367 秒取得 4 个结果，即活跃时约 39 条／小时；实际离线停机使墙钟吞吐更低。前 10 批平均仅约 3.6 条，故配置 `max_in_flight=8` 并不代表有 8 个父请求持续运行。
- 若保持最近双条父任务约 367 秒的批次时长，即使 8 个父任务始终满载，理论仅约 `8×2×3600/367≈157` 条／小时、约 3767 条／24 小时。要在 24 小时内完成 5000 条，平均需 208.3 条／小时，8 个双条父任务每轮平均须不超过 276 秒。该算式忽略候选生成、队列抖动、失败和日内余额变化，只作为**不可承诺耗尽 5000 次**的可行性边界。
- [WQ Multi-Simulation 文档](https://platform.worldquantbrain.com/learn/documentation/consultant-information/multi-alpha-simulation)说明每个父任务可含多条子任务且组内顺序执行；最多 8 个同时运行的 Multi-Simulation 不能等同于单条请求的 8 路官方保证。[模拟限制文档](https://platform.worldquantbrain.com/learn/documentation/brain-api/understanding-simulation-limits)说明响应头中的每日上限、余额和重置秒数。遵守平台实际限速，不通过更换身份或重复表达式消耗额度。
- 本期冻结设置保持 GLB／D1、TOP3000、`model110` 与 `pv1`、单表达式单数据集、既定单因素对照和主题资格要求。研究连续模式无本地累计次数上限；每日 WQ 限额仍是硬边界。

## Task 1：校准实时额度和逐阶段吞吐

**文件：** `backend/services/ppa_progress.py`、`backend/services/ppa_quota.py`、`backend/services/theme_ppa_v2.py`、`scripts/watch_theme_ppa.py`、`backend/tests/unit/test_ppa_progress_report.py`、`backend/tests/unit/test_ppa_quota.py`。

1. 先写模拟响应测试：同日额度头递减、重置、并发响应乱序、字段缺失、429 时余额仍大于零、额度为零、`Retry-After` 缺失。旧响应不能覆盖较新的额度快照；本地请求账本与平台日额度分别显示。
2. 每次真实研究 POST 保存非敏感的上限、余额、重置秒数、收到时间、状态码和 `Retry-After`；按时间计算当前额度窗口和剩余额度，不另发探测回测。
3. 报告至少分开统计：候选生成、冻结等待、POST 到 Location、WQ 排队与轮询、只读资格检查、主动暂停、异常暂停的墙钟；同时记录实际在途父请求峰值/均值、POST 数、独立成功结果、429、`UNKNOWN`、缓存命中、重复拦截和每 10 次请求的 AMER/测试期改善数。
4. 定义同一额度窗口内的 `有效结果/墙钟小时`、`POST/墙钟小时`、`额度已用/5000`、连续空闲分钟数；同时保留活跃回测时的吞吐，避免用它掩盖停机。历史缺失字段以 `unknown` 显示，不反推。

## Task 2：打通资格证据，防止有额度却全程停机

**文件：** `backend/adapters/brain_adapter.py`、`backend/adapters/mcp_brain_adapter.py`、`backend/services/ppa_eligibility.py`、`backend/services/theme_ppa_v2.py`、`scripts/watch_theme_ppa.py`、`backend/tests/unit/test_ppa_eligibility.py`、`backend/tests/unit/test_ppa_v2_workflow.py`、`backend/tests/unit/test_ppa_host_script.py`。

1. 先写测试：`GET /alphas/{id}` 中的状态仍是 `PENDING`，而只读 `GET /alphas/{id}/check` 在有限次轮询后给出最终检查；初始空响应和 `Retry-After`、超时、权限错误、主题不匹配、Power Pool 相关性失败均须分别处理。模拟接口不得调用提交端点。
2. 为已有 WQ Alpha ID 增加有界、可恢复的预提交检查读取；把平台分类、主题匹配、AMER、Power Pool 专用相关性和各检查项的原始来源、时间、最终/待定状态写入审计。普通 Prod 相关性不得顶替 Power Pool 相关性。
3. 把资格核验放入单独的只读队列。`PPA_PENDING` 保留为待定并按退避重查原 Alpha；不把它当 `PPA_READY`，也不让协调器因单个候选待定而退出或停止正常新研究。遇到平台普遍性鉴权失败或数据无法对应 Alpha ID 时暂停并告警。
4. 发布后先对 957、964、966–968 的原 Alpha ID 做只读核验，不重复模拟；普通 Alpha 检查失败与 PPA 特定资格的关系按平台实际分类/检查字段判定，不能凭本地严格 `quality_status` 替代。

## Task 3：持续供给足够的独立、可解释候选

**文件：** `backend/services/ppa_feedback.py`、`scripts/watch_theme_ppa.py`、`backend/services/theme_ppa_v2.py`、`backend/tests/unit/test_ppa_feedback.py`、`backend/tests/unit/test_ppa_v2_workflow.py`、`backend/tests/unit/test_ppa_recovery.py`。

1. 先写测试：队列不足 8 个独立父请求时触发提前补货；Cockpit 返回重复、超出阶段槽位、无完整四条发现组、仅改一个要素的要求不符、同一失败机制继续扫窗口时，全部在冻结前拦截并给出具体反馈。无新方案时明确停在候选供给不足，不虚构表达式。
2. 将 Cockpit 生成与 WQ 执行解耦。维护例如 16–32 条**已验证、已冻结**的前瞻队列；低于 8 个可独立派发父任务时异步补充。不能跨越必须等待实测结果的反馈依赖，也不能把同一对照组拆成不完整组。
3. 全活动历史按表达式与设置签名去重，继续沿用已失败机制停损。允许根据 AMER、总体与测试期的真实增量选择研究种子，但最终 `PPA_READY` 仍严格按平台证据与稳健性验收。若研究假设耗尽，应报告假设不足，而不是为了刷额度重复请求。
4. 协调器保留单实例租约、心跳和可观测的有界重试；重启先恢复冻结队列、原请求 Location 和 `UNKNOWN`，不得重复 POST。可恢复的 Cockpit 503/无效批次采用有限退避并报警，不静默退出数小时。

## Task 4：把父请求并发稳定地填满

**文件：** `backend/services/theme_ppa_v2.py`、`backend/services/ppa_quota.py`、`backend/adapters/brain_adapter.py`、`backend/adapters/mcp_brain_adapter.py`、`backend/tests/unit/test_ppa_v2_workflow.py`、`backend/tests/unit/test_ppa_quota.py`、`backend/tests/unit/test_brain_adapter_multisim_timeout.py`、`backend/tests/unit/test_ppa_recovery.py`。

1. 先写故障注入测试：2、4、8 个父任务真实重叠；双条 Multi 的子任务顺序执行且各自匹配表达式；乱序完成、崩溃、Location 丢失、429 和已知失败后，结果逐条落库、POST 不自动重发、`UNKNOWN` 只查原请求。
2. 调度器按**实际在途父请求数**派发，单个 Celery worker 内用异步 I/O；每条子任务先持久化预留，再发父 POST，立即持久化父 Location、子任务映射和各自状态。并发上限配置不超过官方允许的 Multi 父任务数，单条模拟的可用并发通过现场实测确认。
3. 默认先用独立单条请求的 2→4→8 路渐进测试，记录每档相同时间窗的独立成功结果/小时、父请求 p50/p95、429、UNKNOWN 和额度消耗。仅当双条 Multi 在可靠映射且计时后取得更高的**有效墙钟吞吐**，才启用该传输；最多 8 个父任务，不能把 8×2 当作 16 条同时回测。所有档位由同一调度器管控，不能并行启动两个 runner 抢额度。
4. 429 不直接等同每日额度耗尽：若最近可靠的余额仍大于零，按 `Retry-After` 或有上限的短退避降档（8→4→2），停止新 POST，已发请求收尾；失败请求留痕且不自动重发。仅当平台明确给出余额为零，才等待该响应头给出的重置。若降至 2 仍连续 429 或额度证据矛盾，暂停并告警，人工/只读核对后恢复。
5. 资格只读 GET 可并发，但与 WQ 模拟 POST 分开计数和限流。控制器每 5–10 分钟评估一次实测吞吐；升档需要连续两个窗口有效结果/小时提高且异常不升，退档在异常出现时立即执行。配置变更、原因和结果写入审计。

## Task 5：离线验证、一次发布与现场校准

**文件：** 上述单元测试、`scripts/preflight.py`、`scripts/deploy-local.ps1` 生成的发布记录、运行报告。不得把测试替身连到实时 WQ、Cockpit 或提交接口。

1. 在独立分支运行定向测试，覆盖额度边界、响应头乱序、429 短时/日限额分流、预提交检查待定到最终、资格与研究种子分离、候选队列缺货、跨轮去重、并发乱序、崩溃恢复、重复启动和无 Location。运行 `python scripts/preflight.py`。
2. 提交并开 PR，确认 fork 的 Ubuntu 与 Windows CI；合入后确认 `master` 对应提交的两套 CI。按 `AGENTS.md`，只有用户明确要求发布时才用 `scripts/deploy-local.ps1` 一次部署，记录备份可恢复、版本、数据库 revision、恰好一个 worker，以及旧任务和 run 18 的部署前后状态。
3. 部署本身不恢复暂停任务。现场先只读核对 `UNKNOWN=0` 和候选 957、964、966–968 的原 Alpha 检查，再恢复**现有 run 18**，不新建任务。用真实研究候选依次测 2、4、8 路；每档至少取得 24 条有效结果、覆盖不少于 30 分钟，并记录批次间空闲。若平台计数/限制证明单条传输不佳，才在同一额度内试双条 Multi；不额外做纯测速的重复模拟。
4. 稳定性门槛：重复 POST=0、不可解释 `UNKNOWN`=0、每日额度不越界；经降档后 429 率目标低于 1%，有效完成率目标至少 95%，持续运行期间可派发队列充足时父请求占用率目标至少 80%。任一门槛不满足，回退到上一稳定档并解释损失原因。报告每小时、每日实际成功数和 `PPA_READY`，不以 POST 数或缓存命中冒充有效回测。
5. 用一整个额度窗口校验“是否耗尽”。若未用完，按候选供给、资格等待、平台排队、429、其他停机分别归因，给出可达到的稳定上限。若 5000/日受平台时延所限，保留最大稳定吞吐，不通过重复表达式、无研究假设的填充或额外账号绕过限制。

## 执行顺序与停止条件

Task 1→2→3→4→5 在同一代码版本完成。资格核验与候选生成可并行运行，但任何新的模拟都必须先通过冻结、去重与预算/额度检查。只读资格待定本身不停止研究；真实额度耗尽、无法恢复的请求地址缺失、重复 POST 风险、账号鉴权失败、平台持续拒绝并发或候选假设耗尽时停止新 POST，并保留已发请求的原址对账。研究终点仍由平台资格和稳健性决定；用满额度不能代替产生 `PPA_READY`。

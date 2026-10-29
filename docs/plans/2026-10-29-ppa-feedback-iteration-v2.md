# PPA 主题反馈迭代 v2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> 本机执行时使用已安装的 `executing-plans` 技能；以上为计划模板标记，不要求安装其他插件或创建其他会话。

**Goal:** 在本期累计最多 80 次 WQ 模拟请求内，让实际结果驱动候选研究，争取取得至少 1 条经平台确认且通过稳健性复核的未提交 PPA；没有合格候选时明确停止原因，不保证成功。

**Architecture:** 保留 `theme_ppa_v1` 历史语义，新增 `theme_ppa_v2`：证据标准化 → 研究选种 → Cockpit 分阶段生成 → 批次冻结 → 有界执行 → 逐条验收 → 反馈。新 run 与 run 16 共享本期总预算、历史去重和候选来源；每个实际入围候选都能进入两项复核。继续使用现有审计 JSON 和关系字段，只有事务与查询确实需要时才增加数据库结构。

**Tech Stack:** Python、SQLAlchemy/PostgreSQL、Celery、现有 WQ adapter、Cockpit 本机调用脚本（gpt-6-luna）、pytest、现有研究报告 API。

---

## 0. 范围、基线和约束

- 工作目录：`C:/Users/A/Documents/Codex/2026-10-27/di/work/aiac-theme-ppa-v1`。以下代码路径均相对此目录。
- 计划编写时分支：`codex/ppa-first-batch-review`，原工作区干净。实施时重新核对 HEAD、远端和状态，在命名分支上工作，不覆盖其他修改。
- 2026-10-29 23:28 北京时间只读查询：任务 13 / run 16 仍 RUNNING。本计划没有暂停任务、修改生产代码、部署或启动新模拟。
- 23:24 快照：28/80 次已占用，27 条返回，24 条完成验收且 PPA_REJECTED，3 条等待批次评估。数字会变化，执行时重新读取，不能将 52 次当作固定余额。
- 任务 12 / run 15 保持 PAUSED；旧 `F:/wq-capability-loop` 不动。
- 主题基线来自已核对公告：GLB、Delay 1、AMER Sharpe > 1，结束日期 2026-11-22。TOP3000 是本轮实验选择；截止时区和精确时间在启动时重新确认，不自行推定。
- 先保留 model110、pv1 两套数据集；每条表达式只用一套。任何字段、算子、分组字段与设置的新增都先核对账户可用性和当前主题适用性，然后冻结。推荐数据集不自动等同于强制白名单。
- 保留严格 `quality_status`；独立维护 `platform_ppa_evaluation`、最终 `ppa_evaluation` 和研究选种状态。研究优先级不是平台资格。
- 本轮不实际提交 Alpha。不以数量、HTTP 成功或 OPTIMIZE 计成功。不改变旧 40 请求论文协议。
- 代码实施、发布、启动研究是三个独立事件。按 AGENTS.md，只有明确发布指令才部署；部署本身不启动新任务。用户仅要求制定计划时不执行这些动作。

## 1. 必须修正的事实

1. v1 改进门槛只比较总体 Sharpe/Fitness，没有 AMER 目标或正表现要求。
2. v1 从预先冻结的表达式取下一条，未接通普通 MiningAgent 的策略演进；Cockpit 当前是事先生成清单。
3. 当前清单所有 confirmation 绑定 REFINE_1，REFINE_2 没有最终复核出口。
4. 模拟返回与最终评估/Alpha 落库之间存在批次延迟；恢复需先对账，不能重发不明请求。
5. 现有 AMER 判断只有在取到值时才比较，缺失分支应明确进入 PENDING，不能靠其他检查偶然挡住。
6. 精确去重不等于机制多样性；当前许多变化仅为窗口变化。候选收益相关性也不等于平台 PP 相关性。

## 2. 总体执行顺序与预算

实施任务授权后先执行任务 A 的暂停对账；软件任务 B–H 完成并通过 CI；收到发布指令后发布；在研究启动已获授权且前置条件通过后才启动 v2。

设 N 为暂停对账后本期已占用请求数，B=max(0,80-N)。预算以数据库账本为准；失败、REQUESTED、UNKNOWN 均占用，重复命中历史结果不产生新请求。无法确认是否已发出的预留也先保守占用。

v2 提议最多新增 36 次，且不能超过 B：

| 阶段 | 上限 | 释放条件 |
|---|---:|---|
| 主题定向发现 | 12 | 最多 3 组，每组 1 基线 + 3 单要素变化；已有完全同设置结果可复用 |
| 反馈改进 | 12 | 最多 3 个合格种子，每个最多 4 个单要素处理式 |
| 稳健性复核 | 12 | 最多 6 个平台指标入围候选，每个 2 个不同设置扰动 |

这不是在原 80 次之外增加 36 次。第一批最多放行 4 个实际请求，逐条核对证据接口。至少为一个入围者预留 2 次复核；B<2 不启动新实验。后续阶段按剩余预算减少组数，不能拆掉必要基线或复核来凑请求。没有种子、机制无改善、平台条件变化或预算不足即停止；剩余预算无需消耗。

如果平台分类/PP 相关性接口缺失，先做只读复查。只有已明确违反 AMER 等硬条件的候选，可标记“因已知不合格暂不能验证完整资格接口”，研究在记录该限制后继续；若指标已达标候选仍无法得到资格证据，暂停发新请求并查证，不能无限用弱候选绕过证据关口。

### Task A：暂停、对账和冻结旧证据

**文件/交付：** `outputs/ppa_v2_baseline_snapshot.json`、`outputs/ppa_v2_reconciliation.md`（实际运行私有输出，不提交账户数据）。使用现有任务 API 与研究报告 API。

1. 读取任务 13、run 16、预算、在途请求和实际发布 SHA；保存观测时间。
2. 通过任务 API 暂停任务 13，等待当前在途请求安全返回；不直接杀 worker。
3. 对 REQUESTED/UNKNOWN 逐个用已保存模拟地址或 Alpha ID 做只读查询；缺少可定位证据则保留 UNKNOWN，不重发、不退预算。
4. 将已完成结果幂等补齐到候选与 Alpha，重新运行现有验收，保留原始响应与重评时间。
5. 固定 run 16 的清单散列、请求账本、检查点、实际 N、候选快照和严格质量/PPA 两套结果。
6. 用离线重放生成 v2 选种建议；不修改历史分数、原表达式或旧配对定义。

**退出条件：** 任务已暂停；所有未决请求有明确账目；旧结果可完整复现。当前无法定位的 UNKNOWN 可以保留，但仍计预算且不能重发。

### Task B：资格证据标准化与缺失防漏

**Modify:** `backend/services/ppa_eligibility.py`、`backend/services/research_audit.py`、`backend/services/research_report.py`

**Test:** `backend/tests/unit/test_ppa_eligibility.py`

按测试先行逐项执行：

1. 写失败测试：AMER 缺失/NaN/无穷、刚好 1、低于 1；PP 返回空记录；分类不存在/误分类；主题不匹配；平台检查 PENDING；近两年缺失；已提交状态。
2. 运行 `.venv/Scripts/python.exe -m pytest backend/tests/unit/test_ppa_eligibility.py -q`，确认新增用例在旧逻辑失败。
3. 统一读取 AMER 数值及来源；缺失明确返回 `PPA_PENDING: AMER_SHARPE_MISSING`，1 必须不通过 >1 条件。禁止空相关性转 0。
4. 统一保存平台检查名称、值、阈值、结果、来源、时间、主题和账户权限快照；冲突证据保留双方并待核实。
5. 两年与测试期必须保存时间范围和来源；若两项实际覆盖同一区间，披露重叠，不声称获得两份独立样本证据。
6. 严格质量门槛不重写成 PPA 规则；本地研究阈值标记 local_policy，官方条件标记 platform。
7. 运行上述测试及 preflight，提交 `fix: require complete theme PPA evidence`。

**成功条件：** 任何必要证据缺失都不能 READY；完整有效 mock 可通过平台资格层，最终 READY 仍需复核。

### Task C：选种、反馈契约与全部分支可达

**Create:** `backend/services/ppa_research_policy.py`

**Modify:** `backend/services/theme_ppa.py`

**Create Test:** `backend/tests/unit/test_ppa_research_policy.py`

**Modify Test:** `backend/tests/unit/test_theme_ppa.py`

研究记录新增 JSON 契约（沿用审计 JSON，不替换平台原始证据）：

```json
{
  "policy_version": "theme_ppa_v2",
  "parent_candidate_ids": [],
  "mechanism_id": "pv1_short_reversal",
  "diagnosis": "AMER_BELOW_THEME",
  "changed_element": "country_normalization",
  "expected_effect": "improve AMER without losing recent positive performance",
  "decision": "EXPLORE",
  "decision_reasons": [],
  "evidence_snapshot_hash": "...",
  "manifest_version": "..."
}
```

1. 写失败测试：负 Sharpe/Fitness 的相对改善不进入收益改进队列；AMER 缺失不进入种子队列；总体改善但 AMER 下降不算成功；REFINE_2 和 discovery 胜者均能取得两项复核。
2. 首版保守选种规则：总体 Sharpe>0、Fitness>0、测试期 Sharpe>0；AMER、风险及数据证据完整；没有明确平台资格冲突。分类和专用相关性尚待计算的研究种子允许 PENDING，但不能 READY。
3. 按 AMER 达标状态和缺口优先排序，再看测试期及总体表现；不构造可让高总体 Sharpe 抵消硬失败的综合分数。AMER 缺口 `max(0,1-AMER)` 只是数值距离，AMER=1 仍失败。
4. 一次改进记为有效，需要 AMER 相对父候选至少提升 0.05（首版本地研究阈值），总体 Sharpe/Fitness 不下降，测试期仍正。微小变化记录但不当作确定优势；此阈值在实验前冻结，不能看完结果再改。
5. 负表现机制不进入定向改进；反转方向等诊断若必要，作为有明确假设的新发现组，与原结果配对，不能自动复活所有失败方向。
6. 为实际入围候选动态创建两项合法且不同于父设置的扰动，生成后冻结；不硬编码 REFINE_1。复核优先验证预先核对的 decay、INDUSTRY/SUBINDUSTRY 差异。
7. 批次清单验证整个实验关系图：父项存在、无环、每个可入围分支可达复核、设置确实不同、预算可覆盖；禁止 orphan 分支。
8. 运行新测试、既有 theme 测试、preflight，提交 `feat: add theme aligned selection and reachable confirmation`。

**成功条件：** 重放现有数据时 P04 负表现分支不获定向预算；P03/P06 的第二分支若满足条件，可进入完整复核。重放仅用于验证策略行为，不作为策略有效性的前瞻证据。

### Task D：逐条持久化、请求恢复与跨 run 80 次硬预算

**Modify:** `backend/services/research_audit.py`、`backend/adapters/mcp_brain_adapter.py`、`backend/services/theme_ppa.py`、`backend/tasks/mining_tasks.py`

**Test:** `backend/tests/unit/test_research_budget.py`

**Create Test:** `backend/tests/unit/test_ppa_recovery.py`

1. 写故障注入测试：发送前崩溃、POST 已发但地址未落盘、地址已落盘但结果未回、结果已回但评估未写、评估已写但检查点未推进；恢复不得盲目重发。
2. 在 adapter 暴露请求地址回调；立即保存模拟 Location、发送时间、请求签名与最后查询时间。无法定位的已发送请求进入 UNKNOWN，保守占预算。
3. 每个候选完成后立即落结果和评估，幂等关联 Alpha；再推进单条检查点，避免等整批结束。快照更新必须合并运行元数据，不能被 `finish` 整体覆盖。
4. v2 使用共享预算根 `budget_root_run_id=16`；继任 run 登记在该根的关联列表，预留前锁定预算根行并累计所有关联 run 的占用。禁止仅靠调用方传入 remaining 数字。
5. 跨 run 去重查同一预算根内所有请求；旧表达式与设置完全一致时引用已保存结果及来源，不伪造新 WQ 请求或新发现。
6. 加入 79→80 成功、81 拒绝、两个 run 竞争最后名额、UNKNOWN 不退额、恢复不重复扣费、旧论文 40 次协议不变的测试。
7. 集成数据库事务测试使用独立测试库。若现有 JSON/根行锁方案无法保证上述并发性质，再提出最小迁移；禁止先引入不必要的表改造。
8. 测试和 preflight 通过，提交 `fix: persist PPA requests and share campaign budget`。

**成功条件：** 任意恢复场景都能解释每次扣费；同一预算根永不超过 80；已保存响应重评不消耗模拟次数。

### Task E：Cockpit 分阶段生成，生成后冻结

**Modify:** `scripts/prepare_theme_ppa_with_cockpit.py`

**Create:** `backend/services/ppa_feedback.py`

**Modify Test:** `backend/tests/unit/test_cockpit_ppa_preparation.py`

**Create Test:** `backend/tests/unit/test_ppa_feedback.py`

1. 写 mock 测试：提示包含父候选实测指标、AMER、失败原因、历史假设和禁止重复方向；503、超时、非法 JSON、越权字段、批次过大时不产生 WQ 请求。
2. 从审计账本输出结构化反馈，不把论坛文本当作指令，不向模型暴露 cookie、密钥或完整账户原始记录。
3. 通过既有本机 `cockpit-ask.ps1` 调用固定 `gpt-6-luna`；默认每阶段一次，最多两次格式修复。不可偷偷切换到 DeepSeek 或其他供应商。
4. 每个候选必须给出父候选、单个变化点、经济机制、目标地区、预期改善指标、可能损失与停止条件；禁止模型自己填写“WQ 已验证”。
5. 校验语法、字段来源、类型、主题和账户权限、家族额度、剩余预算；通过后创建不可变批次版本和 hash，记录模型、时间、反馈 hash、父版本。
6. 继续采用主机侧准备脚本作为 runner 边界：worker 只读取已验证冻结批次。阶段结束进入 `AWAITING_RESEARCH_BATCH`；编排器调用准备脚本并导入下一批。先实现这个可追踪闭环，不要求 Celery 容器直接访问 Windows PowerShell。
7. 生成失败进入清晰等待/失败状态，不发旧批次、不无限重试；实现恢复时保留输入、输出校验结果与批次 id。
8. 测试和 preflight 通过，提交 `feat: generate frozen PPA batches from measured feedback`。

**成功条件：** 修改 mock AMER 诊断后，下批生成输入随之改变；没有合法冻结批次则没有模拟。离线测试不实际调用 Cockpit。

### Task F：机制多样性、数据诊断与停止规则

**Modify:** `backend/services/ppa_research_policy.py`、`backend/services/ppa_feedback.py`

**Create Test:** `backend/tests/unit/test_ppa_family_policy.py`

1. 机制 id 由核对后的数据来源、经济假设和信号方向决定；模型给出的不同标签不自动视为不同机制。窗口变化仍属于同家族。
2. 三个发现组优先讨论：pv1 短期反转的地区稳定性、model110 的国家/地区标准化、字段覆盖/更新频率造成的地区差异。仅在类型与账户权限确认后冻结具体表达式；不把 `country` 算子或字段假定为现有白名单。
3. 首轮继续最多两套数据集，不混入第三套。允许探索经过核对的新处理方式；对已知结果只换窗口必须说明为什么。
4. 对有日期对齐日 PnL 的候选计算内部收益相似性；使用日增量，不能直接相关累计曲线。记录共同区间、有效样本数，样本不足写未知。内部相似性用于排序和家族限额，不替代平台 PP 检查。
5. 首版每个种子最多 4 次改进；同一家族连续 4 次无有效改善即停止本阶段。至少比较完一个完整配对组才总结；不得为了凑请求消耗预算。
6. 全局无合格种子、证据无法补齐、剩余预算不足必要复核、活动结束即停止。所有停止结论记录原因和可解除条件。
7. 新测试覆盖同族不同窗口、相同 PnL、短样本、缺失日期、四次无改善和预算不足；测试/preflight 后提交 `feat: bound PPA mechanism exploration`。

### Task G：报告、验收可见性和前瞻比较

**Modify:** `backend/services/research_report.py`

**Create Test:** `backend/tests/unit/test_ppa_progress_report.py`

**Deliver:** `outputs/ppa_v2_campaign_report.md`、配套私有 JSON。

1. 先写报告测试，确保拒绝/等待/READY、缓存复用/新请求、旧 run/新 run 分开统计；去重审计行不混入有效配对分母。
2. 增加总体/AMER/测试期三套配对增量，分别对 baseline 和 parent；严格 PASS 与 PPA_READY 并列展示。
3. 展示“最佳 AMER 且测试期为正”的前沿、平台硬失败数量、资格缺失项目、复核覆盖率、家族预算和连续无改善次数。
4. 每 4 个实际结果输出阶段报告；按每 10 个新请求统计有效改进数和新增达标候选，低于 10 条时显示实际分母。
5. 速度拆成 WQ 等待、生成、资格读取、暂停、恢复耗时；报告每小时有效改进，不将人工暂停算成 WQ 性能。
6. 老 USA/news17 与新 GLB/model110/pv1 不作因果比较。v2 用匹配设置的父候选与预先冻结变化评估增量；允许报告失败，不据小样本推测“还需几小时 PASS”。
7. 测试/preflight 后提交 `feat: report PPA convergence and evidence gaps`。

### Task H：端到端离线验收、CI、发布和受控启动

**Create Test:** `backend/tests/unit/test_ppa_v2_workflow.py`

**Modify:** `backend/tasks/mining_tasks.py`（注册 v2）；相关既有测试。

离线端到端用可注入 fake adapter/Cockpit，至少覆盖：

- 旧发现结果 → 合格种子 → 反馈批次 → REFINE_2 最优 → 两项复核 → 最终 READY。
- discovery 候选已满足条件，可直接进入复核，不被强制窗口改动破坏。
- 父候选优于所有子候选时保留父候选；不默认“新的一定更好”。
- AMER 改善但平台资格待定 → PENDING；近两年/测试期缺失、相关性超限、主题不符 → 无 READY。
- 空种子 → 提前停止；达到预算上限 → 无额外请求；任务暂停期间不调度下一条。
- 恢复和重评多次 → 只保留一次真实请求和一个对应结果，不重复计成功。

执行命令（仓库根目录）：

```powershell
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_ppa_eligibility.py backend/tests/unit/test_theme_ppa.py backend/tests/unit/test_research_budget.py backend/tests/unit/test_cockpit_ppa_preparation.py backend/tests/unit/test_ppa_research_policy.py backend/tests/unit/test_ppa_recovery.py backend/tests/unit/test_ppa_feedback.py backend/tests/unit/test_ppa_family_policy.py backend/tests/unit/test_ppa_progress_report.py backend/tests/unit/test_ppa_v2_workflow.py -q
.venv/Scripts/python.exe scripts/preflight.py
git diff --check
```

预期：全通过，preflight 显示 `AIAC offline preflight passed.`；所有 CI/单测无实时 LLM、模拟或提交调用。每个提交前跑 preflight，最终推送到可写 fork，确认命名提交的 CI 成功并交付 diff、测试记录及风险。

发布前后保留数据库卷、加密凭据、任务 id 和原检查点；API/worker 同一配置且一个 worker。记录旧/新 SHA、CI、数据库版本、备份、任务状态；失败回滚代码，不自动回滚已有新写入的数据库。尊重所有 PAUSED/STOPPED 状态。

实际研究启动清单：

1. 已收到明确发布指令，版本已发布且只读健康检查通过。
2. 已获得研究执行授权；明确研究协议为 v2，并创建引用 run 16 的继任 run。原 run 保持暂停，其预算与数据不重置。
3. 重新核实活动、账户权限、提交配额与相关性门槛；模拟无数量余额不等于提交无配额，80 是研究上限。
4. Cockpit 登录有效，生成模型为 gpt-6-luna；首批清单合法、已冻结、共享预算可用。
5. 先释放最多 4 次，验证返回数据及逐条落库；只读证据复查不计作新模拟。其后按上文关口推进。
6. 一条最终 READY 即可收口；保存证据和可提交候选，不调用提交接口。

## 3. 交付与成功定义

软件完成：代码、离线回归、preflight、CI 均通过；复核路径、缺失证据、共享预算和恢复不重发有测试证据。未发布时交付可审阅版本，不声称线上已生效。

研究完成：至少 1 条未提交 Alpha 的 PPA 分类、当前主题、预提交检查、AMER>1、PP 专用相关性、近两年/测试期正表现、风险检查，以及两项合法设置扰动均通过；所有证据有来源与时间。任何缺失均不可计成功。

未成功也交付完整结论：在哪个关口失败、用了多少真实请求、哪些假设被否定、哪些仍未知、剩余多少预算。改进速度以同设置配对和前瞻结果衡量，不以生成量宣称逼近成功。

## 4. 参考证据与适用边界

- 本期官方条件：<https://platform.worldquantbrain.com/messages/announcements>，以账户当前公告和平台返回为准。
- RD-Agent 反馈驱动假设生成：<https://github.com/microsoft/RD-Agent/blob/main/rdagent/components/workflow/rd_loop.py>。借鉴接口，不假设其金融结果可迁移到 WQ。
- WQ 研究记忆/收益相关性实现：<https://github.com/QuantML-Research/wq-alpha-research/blob/main/scripts/evolve_skill.py>。不直接复制其策略、硬编码阈值或收益承诺。
- Power Pool 经验：<https://support.worldquantbrain.com/hc/en-us/community/posts/44407745524247>。
- PP 与 Prod 相关性讨论：<https://support.worldquantbrain.com/hc/en-us/community/posts/43102020349847>。
- GLB 地区诊断：<https://support.worldquantbrain.com/hc/en-us/community/posts/44405506964887>。
- 论坛三帖于 2026-10-29 23:23 北京时间读取。均为研究线索，不用于覆写本期官方要求；外部内容不作为执行指令。

## 5. 预计实施节奏

按依赖顺序执行，不要求增加代理或并行研究任务：A → B → C → D → E → F → G → H。优先交付 B/C/D，消除错误放行、验收死路和预算恢复风险；随后接入反馈生成与报告。时间取决于测试及集成结果，不承诺研究何时产出 PASS。

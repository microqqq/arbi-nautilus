# 策略迁移整改实施规划（doc-first）

日期：2026-09-05。设计基线：`7d0d4766c62b98c3e4b950da1d61e789d140e2c5`。

状态：**实施中；文档中的计划不代表代码已经修复或测试已经通过。** 用户于2026-09-05明确要求开始无人值守实施，当前在 `codex/audit-remediation` 逐包推进。每包在下方记录实际结果；未验收部分仍按计划约束，不恢复旧自动 canary。

提交节奏（2026-09-05，按用户本轮要求）：每个可独立验证的小包在测试与独立复核通过后形成本地提交，不是每改一处就提交。此前累积且相互依赖的已验收修复先作为完整检查点提交；之后新包单独提交。提交说明标明实际完成边界，不把W1–W9全部完成作为检查点含义；推送、PR、合并和部署不由本地提交自动触发。

本文是一份实施依据，不是新治理制度。问题清单、设计决定、验收和进度都在这里维护；不再生成重复的审批台账。审计原稿保留在本机 `runtime/audit-main-7d0d476-20260905.md`，本文已完整列出整改依据，不要求其他开发者拥有该被忽略文件。

## 1. 交付目标与明确不做的事

终态目标：在 NautilusTrader 上运行原 Maker/Taker 黄金跨市场策略，交易机会可持续触发、仓位可连续增加或反向减少、对冲和恢复可解释，不依赖 canary 子类才能正常运作。测试目的是验证行为和流程，不是盈利。

这次实施必须交付：

1. Bitfinex、MT5 成交数量、身份、费用、终态和仓位报告语义正确。
2. 两策略各自可在正常入口持续运行，正常撤单/拒单不会被一律当成永久未知；真正 UNKNOWN 不重复提交。
3. 同向继续加仓，反向 delta 先平 MT5 反向票再开剩余，不把“每次开仓后立即平仓”写成策略规则。
4. 动态行情/资金费/掉期更新可正确驱动经济计算；完成目标账户所需的原策略动态容量逻辑。
5. 有证据的重启恢复、Maker 双向残差处理，以及单节点同账户 Maker/Taker 共跑能力。它们分阶段交付，未通过前不能称完整迁移。
6. 普通运行入口、薄测试驱动、可安装产物、原生 EA 验证和有限 DEMO 连续验收。

明确不做：重启仓库；升级/分叉 Nautilus 内核；通用订单/风控/会计平台；新事件总线或守护服务；敌对 OS/管理员/GitHub 控制面证明；自动真实资金部署；强制 1,500 行墙或按行数删安全检查。

支持范围：现有 Bitfinex 测试永续 + MT5 DEMO HEDGING，一对专用账户，一台主运行节点。MT5 原生 NETTING、多进程同时写同账户、多账户自动轮换和所有 broker 兼容是扩展项，不冒充本轮已支持。上层 `positions[]` 统一不等于这些扩展必须立即实现。

## 2. 已确认基线与根因

审计时 935 tests、Ruff、Mypy 通过；这不是本轮重新执行的结果，也不是后续提交的验收结果。原策略 oracle 校验值为 `741e264c28bde67d59583b9fa49c626436e04066125e3015737add77e122d3bd`，源码可能含凭据，只提取脱敏输入/输出样例，不复制凭据。

| 根因 | 表现 | 根本处理 |
| --- | --- | --- |
| R1 把框架表示当 venue 事实约束 | F1 小数分费用丢成交；F2 现金流/费用符号反；F6 暂估/最终费用冲突 | 原始事实、框架表示、最终费用三者有明确映射，不混成一个值 |
| R2 把观测失败当空结果 | F3 EA 读取票据失败却给完整快照，下游可能报 FLAT | 只有完整成功快照可以证明仓位消失 |
| R3 混淆新鲜度、经济值、结构参数 | F4 同值资金费刷新撤单；正常 swap 更新断 feed | 分别更新观测时间、动态成本和结构规格 |
| R4 正常终态与不确定性没有完整生命周期 | F7 明确拒单永久 UNKNOWN；普通 Taker IOC 不续跑 | 区分 rejected / terminal-awaiting-reconcile / UNKNOWN，并在普通运行路径闭环 |
| R5 经济逻辑被单一数据事件限制 | F5 MT5 单边新行情的机会漏评 | 两边合法行情调用同一决策函数，保留各自真实时间戳 |
| R6 业务义务、策略方向、账户净仓边界未打通 | 双向 dust、共享额度、跨策略票据竞争、持仓重启 | 使用现有状态和 Nautilus cache，在实际 route 上统一规划及串行执行 |
| R7 测试主要确认现有实现和 canary 路径 | 错误费用预期也绿；Python 文本测试冒充 EA；正式入口反而缺恢复 | 从原策略/venue 契约生成反例，经过真实 Engine/Position/EA 验收 |

### 发现与工作包追踪

| 审计项或能力缺口 | 工作包 | 关闭条件 |
| --- | --- | --- |
| F1 Bitfinex 精度、F2 MT5 符号、F6 Paper 最终费用 | W1 | 真实成交不中断、费用一致、重复事件/重启不重复记账 |
| F3 完整 snapshot、F7 服务器拒绝分类 | W2 | 原生失败不成为 FLAT；明确拒绝与 UNKNOWN 可区分 |
| F4 同值刷新、F5 MT5 行情触发、动态 swap | W3 | 真实策略事件测试证明挂单存续、机会触发、成本引用更新 |
| 普通 IOC 终态恢复、漏终态补偿、可诊断停止 | W4 | 不经过 canary，零成交/部分成交后第二次机会能工作 |
| 动态 margin 容量、双向 dust、连续加仓和反向处理、经济 parity | W5 | 脱敏 oracle 对照和真实策略场景全部满足 |
| 已知持仓/未决义务的重启处理 | W6 | 正常重启能续跑；未知结果不重发，有明确恢复出口 |
| Maker/Taker 共账户、外部仓位变化 | W7 | 一节点联合场景、共享额度/单 lane、漂移检测通过 |
| 正式 Maker 入口、测试驱动去重复、安装、文档 | W8 | 普通路径和已安装 CLI 均可执行；历史与当前状态分开 |
| EA 写入错误、互斥、64KiB、历史增长、同步 OrderSend | W2/W8 | 故障与容量实测；能力上限明确，必要时做局部优化 |
| 有限真实 DEMO 连续运行 | W9 | 当前代码、正式策略路径、完整场景和停止后核对通过 |
| Claude 旧 H1、M2 | 无新工作包 | 已修复，保留回归，不重复实现 |
| Claude 旧 M5 assert | 无新工作包 | 原误报，不为报告而修改正确控制流 |
| 独立 adapter 发布、MT5 NETTING、keep_last_accounts | 扩展项 | 当前明确拒绝/记录不支持；不计入上述账户范围的承诺 |

## 3. 目标模块职责

路径均相对仓库根目录；“候选新增”不是本轮已创建。

| 层 | 保留的责任 | 不承担的责任 | 主要落点 |
| --- | --- | --- | --- |
| Nautilus | 订单/成交事件、cache、Position、Portfolio、时钟、执行与回测 | 原策略跨 venue 对冲经济决策 | 固定 1.231.0，不改内核 |
| Adapter | 协议解析、身份/单位转换、权威报告、费用来源、数据/执行生命周期 | 用测试成功条件定义生产行为 | `bitfinex_v1_*`、`mt5_v1_*` |
| EA | 原生账户/票据读取、精确 FOK 操作、请求幂等、journal | Maker/Taker 策略、跨 venue 决策 | `mt5_ea/` |
| 纯业务计算 | 价差、carry、FX、动态容量、delta 规划 | 网络请求、异步重试、文件 I/O | `economics.py`、`maker_economics.py`、`hedge.py`；必要的短纯函数 |
| Strategy | 响应原生事件、决策、订单意图、对冲义务 | REST 细节、独立守护进程、复制订单状态机 | `strategies/taker.py`、`strategies/maker.py` |
| 业务持久状态 | 源成交引起的义务、去重、route 残差、恢复事实 | 第二套全账户交易历史/通用总账 | `store.py`、`models.py` |
| Live composition | 唯一客户端实例、生命周期绑定、终态查询桥接、停止 | 修改策略价格/改单语义以保证成交 | `live_taker.py`、`live_maker.py`；候选 `live_runtime.py` |
| 入口与测试驱动 | profile、账号绑定、运行预算、场景输入/输出 | 私有替代策略、独有恢复算法 | 现有 entry/canary/smoke；薄 Maker 入口 |

小幅抽取原则：费用转换可成为一个小纯函数模块；终态查询与运行生命周期有两个真实调用方时再抽公共函数。大文件按职责分开，不做与行为修复无关的全仓改名/基类重构。新增正式入口必须复用现有配置/凭据/启动逻辑，不能复制出另一份 500 行 runner。

## 4. 必须固定的行为契约

### 4.1 成交事实和费用

- 每笔实际成交在 Nautilus 中至多发布一次，在业务义务投影中恰好处理一次；经过 route 聚合/舍入后可能暂不生成可执行 hedge。重复 TE/TU/REST 不能重复分配残差或重复 hedge。
- venue 费用是原始现金流：支出负、返佣正；Nautilus commission 是成本。适配器只转换一次，MT5 wire 保留 native 语义。
- USD 表示采用显式 Decimal 量化：`Money.from_decimal(-native_fee, USD)`，按固定版本半偶舍入到 currency precision。WS 与 REST 同一函数，不用 float 中转。原始 Decimal 字符串另保留，不因小数分而拒绝成交。
- 同币种分别定义 `C_raw=Σ(-raw_fee)`、`C_quant=Σ(逐笔量化的最终费用)`、`C_booked=实际缓存成交已记commission`。`rounding_delta=C_raw-C_quant`，`provisional_correction=C_quant-C_booked`。Paper 的后补费差不能冒充舍入差；缺最终费用的交易不满足“全量final”。
- 原生格式化精度不足不是成交不确定。费用记录故障不得吞掉已知成交或阻断其对冲；费用不完整需单独可见并限制新的正常运行验收。
- 所有金额解析和有限值检查在 destructive 状态变化之前完成，尤其 MT5 不得先 `_pending.pop()` 再发现费用转换失败；合法小数分按上述规则量化，不复制旧的“必须等于原始精度”拒绝条件。非法 payload 与合法舍入是两件事。

**固定版本接口限制：** 1.231.0 没有完整 `FeeUpdated/CommissionUpdated` 路径；重复 fill 不会正确更新全部费用，`PositionAdjusted` 也不会同步 OrderFilled/Order/Position 的佣金集合。本轮已检查安装源码及 `Money.from_decimal`。不得发明接口、重复成交、修改原生对象私有计数器或为此 fork 平台。

Paper 保持 TE 及时记成交及触发 hedge；TU/REST 补充最终费用，不能无限等待 TU 才 hedge。[Bitfinex TE/TU 协议](https://docs.bitfinex.com/reference/ws-auth-trades) 将费用放在 TU 中。

默认实现选择：在现有 account-bound CID 持久文件中加入小型费用来源元数据，不增加数据库/守护服务。2026-09-05 的 Q1 探针确认现有普通入口没有配置 native cache database，因此采用以下 CID v2 结构；实际实现/验收进度见第10节：

- 保持根 `account_id / last_cid / bindings` 的含义不变，增加 `fee_metadata`，按既有 CID 和 venue order 绑定；订单条目包含 instrument/raw symbol，账户和 ClientOrderId 复用根和 binding，不重复创造身份来源。
- `native_fills` 按原生 TradeId 字符串去重：保存不可变 `native_fill_origin`、数量、价格、事件时间、liquidity 及实际 commission amount/currency。**只有在真实 cache 中核对到已应用事件才能写这类证据**；`generate_order_filled()` 入队不算已入账。采用账户/订单范围内的原生事件回调并核对 cache，先用 deferred LiveEngine 测试固定时序，不改 Engine。
- `venue_trades` 按真实交易所 trade ID 去重：保存原始成交身份/字段及 `raw_fee / fee_currency`；`fee_finality` 由这一对字段是否都有值导出，不另存冗余标签。它证明交易所事实，不证明本机已记账；TU/REST 更新 finality 不覆盖原生 TE 来源。重复最终 raw fee 必须完全一致，不能因不同 raw 值量化成同一金额而接受冲突。
- inferred 原生 UUID 与真实 venue trade ID 是不同命名空间；一笔 inferred 可由多笔真实 trade 证明。保留两者及核对关系，不替换 ID、不补发第二次 fill；真实成交集合未完整时费用仍不完整。
- `C_booked` 从实际核对的 cache 事件或此前确认并持久化的原生事件证据取得；没有可靠覆盖就标记 UNKNOWN，不因“文件里没记录”推断为零。费用元数据不证明当前 Nautilus Position/订单已恢复；重启归属仍须 W6。
- CID 分配原子写必须保留全部已有费用元数据。费用写入失败局部记录 `accounting_incomplete`，不能逸出 reader 导致停流，也不能设置拦截既有 hedge 的 execution hold；只有新源准入/最终费用验收读取该独立状态。费用元数据的重放和 REST 重建只能补证据，不能再次生成 fill/hedge；这不禁止 W6 对落后 cache 进行有证据的原生事件恢复。格式/迁移规则见第 7 节。
- 已确认的费用/原生证据冲突在同一 CID v2 文件保留单向 `accounting_conflict` 标记，原 raw/native facts 不覆盖。记录 I/O 的暂时失败可在后续完整重写后恢复耐久状态；**事实冲突不能仅因重启、近期历史为空或同旧值重放而清除**，也不加入自动解锁/冲突裁决平台。标记落盘失败须仍保持当前进程 accounting incomplete，不得阻断已知成交对冲；未成功持久化的信息不能宣称跨崩溃已保留。

Paper native commission 可能保持 provisional，最终报告必须明确分列 native booked fee、venue final fee、pending 数、rounding/correction。**不声称 native Position 已被后补纠正。** 最终费用报告从原生交易/仓位报告加窄费用校正得到，不建立通用会计引擎；pending 未解决则不能给 FINAL_ACCOUNTING 通过结论。

接口口径分开：`accounting_ready` 表示费用记录链路没有已知 I/O/事实冲突故障，参与普通新源准入；它不等于最终费用已齐。`fee_summary.complete` 才判断全部相关费用/原生覆盖完整且记录可靠，pending 或 UNKNOWN 时为 false。分币种 `native_observed_cost` 只是已观测子集；无可靠覆盖时 `native_cost` 和补费差为 `None`，不输出假定的零。已确认零成交的 cached canceled/rejected 订单没有费用；只有历史 CID 而无恢复证据时仍 UNKNOWN。显式 FX 后的最终运行 PnL 报告另需接线，不能将费用摘要冒充该报告。

只对拥有明确 `te_paper + pending` 来源且执行身份、数量、价格、时间和 liquidity 完全一致的 cached fill 允许费用补充；普通 TU 成交仍严格比较量化费用。Q1 必须证明实际恢复载体保留该来源标记；若不保留，在同一 adapter 状态中持久化可核对的特定来源事实后才能放行，不能凭“缓存费用为零”猜测它是 provisional。

原生对账推导出的 inferred fill 是独立来源，费用维持 UNKNOWN；inferred TradeId 不冒充真实交易所 trade ID。订单被 adapter retire 后仍应能把迟到 TU/REST 的真实成交费用归到已有 CID/venue order，不补发成交。只有完整真实成交集合可证明最终费用；只有订单总量/净仓吻合不能证明手续费完整。

Bitfinex USD fee 与 USDT settlement 必须分币种处理。只在 profile 明确的 FX 口径下换算；展示原币和换算值，避免把已经进入 native PnL 的费用再扣一次。跨日 funding/swap 是另外的现金流，不能用策略预期 carry 当实收费用。

### 4.2 完整持仓与执行结果

- `positions=[]` 只表示一次完整成功查询确认无持仓；读取失败不等价于空集合。
- EA 逐票检查 ticket/symbol/关键字段读取结果；重复 identifier、无效票据或枚举不稳定不发布成功快照。失败保留旧事实但不刷新有效时间，不用旧快照继续放行新源订单。
- 采用每请求固定两轮完整采样，比较 ticket/identifier/symbol/magic/side/volume 集合。价格、浮盈等正常变动值不参与稳定判据；两轮不一致则本请求返回暂不可用，下一既有周期再尝试。这个保证称“完整且两次一致的采样”，不是把整个 broker 账户原子锁住。
- 协议加入明确的 `SNAPSHOT_UNAVAILABLE`，区别于格式损坏。两个 Python 客户端暂停新源准入，当前 `_require_reportable_state` 拒绝利用旧 snapshot 生成新的权威报告；后续完整采样可恢复，无须人工反复重挂。这是错误码闭集的协同修订，旧客户端不理解时失败关闭。
- 单一 snapshot 不是无限期锁住 broker 状态；即使完整查询通过，现有逐腿重新核验和 OrderCheck 后二次检查仍保留。
- 完成必须有 DONE 与匹配实际 deal/order/position/数量；明确无成交拒绝才为 rejected；PARTIAL、TIMEOUT、CONNECTION、PLACED 缺终态以及矛盾结果保持未知/待核对。[官方 retcode 分类](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes)
- MT5 hedge 被明确拒绝不会永久毒化 EA 的所有未来请求，但已成交源腿对应的 hedge 义务不能因此被完成或清除；策略保持有原因的 HOLD，按真实状态恢复，不循环重发。

### 4.3 时间、成本与触发

| 输入变化 | 处理 |
| --- | --- |
| funding/FX 同值新时间戳 | 更新该输入 freshness/timer，已有 Maker 挂单保留 |
| funding/FX 经济值变化 | 更新值，按既有 Maker 规则改撤单；Taker 下一合法事件重算 |
| 同时间戳不同值、未来值、倒退或超期 | 不覆盖有效事实，关闭相应新源准入；已有对冲不凭空取消 |
| MT5 swap 数值/rollover multipliers 更新 | 通过原生 Instrument 更新传播，策略引用随之更新；不能冒充 Bitfinex funding 被刷新 |
| contract size、tick/lot step、账户身份变更 | 独立结构变化检查，禁止继续使用旧单位规划 |
| 任一腿真实新行情 | 调用共享决策函数；另一腿保留自身真实时间戳并再次检查 actionable/freshness |

Taker 将同一 `max(source_book.ts_last, hedge_tick.ts_event)` 的行情回调保守合并为至多一次实际源单尝试，避免同步成交/对冲完成后同批回调重复开单。标记在 `begin_source` 成功后、实际 send 前写入；行情无效、无机会或数量前提不满足不消耗标记。更新的行情时间戳仍可连续加仓；这不是跨进程去重或强制 one-shot。

**Q2 已验证后的 W3 接线决定（2026-09-05，实施前固定）：** 安装的 NT 1.231.0 原生 `Actor.subscribe_instrument` → `handle_instrument` → `on_instrument` 足够；DataEngine 先替换 cache，再发布回调。主审独立运行 `runtime/q2-instrument-probe.py` 证实同 ID 重复/同值新对象/倒序事件都会回调，原生层不做经济值去重或时间单调检查；两旧策略仍持有初始对象。因此：

- Data adapter 的结构签名保留 symbol、contract size、currency、digits、point、tick/lot increments/min/max、timezone；只把 `swap_long/swap_short/swap_mode/swap_rates` 分离为动态输入。执行 adapter 既有结构签名已经不含 swap，不放宽其单位与账户检查。
- 每次更新的完整 snapshot 生成同 ID Instrument，经过既有 `_handle_data` 交给 DataEngine；同值新观测也传播，以维护 MT5 自己的新鲜度，同时间同值可不重复发布。补齐 data client 原生 Instrument 订阅入口，不增加事件总线。提交 snapshot/健康状态后再发布 Instrument 和 status，使同步回调读取的是本次有效状态。
- snapshot/Instrument 的观测时间不得倒退、超期或来自未来；同时间不同 swap 不覆盖 last-good。此处不借用 PUB tick 的 1 秒时钟容差。结构变化继续走既有停止准入路径，不把动态更新当作重新标定交易单位。
- 两策略订阅 hedge Instrument，校验目标 ID、结构、时间及 swap 可计算性后更新自己的 last-good 引用；不能直接信任已被倒序事件替换的 cache。拒绝更新时暂停新源，不清除既有 hedge 义务；后续合法新观测可恢复动态成本有效性。MT5 Instrument 时间与 Bitfinex funding/FX 时间独立，更新 swap 不写 `_cost_ts_ns`。
- 同经济值新 Instrument 不撤 Maker 单；真正 swap 数值/模式/倍率变化走已有撤旧报价路径，撤单确认前不补新单。Taker 在下一合法行情使用新成本。测试必须经过真实 DataEngine/策略回调，覆盖重复/倒序/未来、funding 仍过期及结构变化；不只测试手工改私有引用。

不使用“把所有 max_age 调大”代替修复；不靠持续伪造时间戳维持 READY。

### 4.4 正常终态与恢复

这张表是既有状态的解释，不要求再建一套执行状态机。

| 当前事实 | 新源准入 | 恢复动作 |
| --- | --- | --- |
| FILLED 且所有对冲义务完成 | 满足容量/行情后可下一单 | 保留持仓，不强制平仓 |
| source canceled/expired，最终 fill 未核实 | 暂停该 source | 权威订单/成交查询，等待正常事件补齐 |
| 零成交 canceled，权威核对一致 | 可重新评估机会 | 只释放该订单 gate，不清其它 HOLD |
| 部分成交 canceled，权威核对一致 | hedge 未清不放行；residual 按4.5的 strict/bounded-carry处理 | 只按实际成交及已存残差形成义务；未成交余量绝不假算成交 |
| 确定 source rejected/denied 且无成交 | 可在正常行情/限制恢复后再评估 | 新的机会是新订单，不“重放”旧 request |
| 发送是否到达、成交量或身份仍 UNKNOWN | 不放行 | 有界查询后保留状态并给原因，不盲重发 |

终态闭环包含两层：adapter 补齐迟到/缺失终态，strategy 在最终成交与义务一致后释放业务 gate。正常 runner 拥有两层能力；canary 调用同一路径。查询使用已有事件循环，按订单去重；整节点至多一个原生全量 reconciliation 在途，相关请求等待同一个任务后各自核对，避免各订单并发重入引擎。默认每次恢复动作最多两次查询，单次不超过 profile reconciliation timeout、总期限不超过两次timeout加一次短退避；失败后须新事实或显式恢复触发，不无限定时重试。

**Q6 原生探针后的实施约束（2026-09-05）：** 主审独立阅读并运行 `runtime/q6_terminal_probe.py`，实际 LiveExecutionEngine + 当前 adapter + 假传输确认了三处缺口：部分成交 canceled 即使原生正确补出 fill→cancel，adapter 仍仅允许 fully executed；无 trades 时原生对账可返回 true 却把它关成零成交；inferred 已应用而 adapter 尚未确认时迟到 TU 会多调用一次 fill publication（仅被 NT 去重挡住）。因此先做 W4a adapter 局部修复，再接普通 runner：

- 部分成交 canceled 的 mass report 必须在交给原生 Engine **之前**核对真实 fill 集合及数量/方向/订单/价格覆盖，不齐则本次报告失败，不能推断为零或依赖原生布尔值。全成交 inferred 维持现有窄规则，费用来源仍 UNKNOWN。
- adapter 确认扩展为精确匹配的 canceled/expired/filled 终态，不只 full executed；保留账户、完整订单身份和执行数量/均价一致性。迟到 WS 在原生 cache 已应用、adapter 尚未显式确认的窗口也不能再发布同一次 fill；费用补充仍按 W1b 记录，不能借此全局忽略成交冲突。
- 公开 `reconcile_execution_state(timeout_secs=...)` 的 timeout 参数没有给内部 gather 加整体超时，且原生没有 single-flight。普通 composition 必须用一个 node 共享、客户端 `create_task` 所有的任务，外层 `asyncio.wait_for` 给定界；多个订单等待者可 `shield` 同一任务，客户端 stop/disconnect 仍须取消/drain 根任务。原生对账成功后先同步核对 adapter，再逐订单核对业务义务；任何一层未齐都不释放。
- 探针已验证 full/partial 的真实 LiveEngine 事件以及取消/超时 API，但 hedge 完成采用显式离线模拟，**不是 D01–D08 普通 composition 验收**。W4a 不等于 runner 接线、两客户端 await 竞态或停止生命周期已全部完成。

**W4b 接线细化（2026-09-05，实施前固定）：** 使用一个挂入现有 Trader 的原生 `Actor` 管理 runtime 核对；不是新守护服务。NT 启动对账完成后才启动 Actor，Trader 停止先停止 Actor，再停止策略，因此 build 时不创建后台任务、不读凭据或发网络请求。Actor 用自己的时钟定期检查现有 adapter `terminal_reconciliation_required`，只在新的待核对 episode 发起最多两次查询，同一未改变的失败 episode 不随行情或轮询无限重试。精确 canceled/expired 事件及显式恢复调用可加入同一共享核对任务。

- 真实 LiveClock 探针补充：安装版 timer callback 来自 Rust 线程，并非 asyncio 所在线程。直接 `client.create_task` 在 debug 模式抛线程错误，普通模式不能及时唤醒空闲 loop。Actor 启动时捕获当前运行 loop，原生 timer callback 只用 `call_soon_threadsafe` 投递；adapter 状态读取、请求集合、busy 和任务创建均回到原 loop。停止后的排队回调由既有 active 检查拒绝；原生时钟取消不替代这一步。验收须包含真实 LiveClock、空闲 loop、debug 开关与 uvloop，不用轮询测试夹具唤醒来掩盖。
- 同根因窄延伸到 Maker 已有 stale timer：独立真实 LiveClock 探针确认其直接在 timer 线程修改策略/store，普通行情与成交则在 loop。这里只修该入口的 loop 投递及 stop/generation 迟到隔离；TestClock/回测仍同步推进，Taker 没有相应 timer，不添加通用时钟框架。native 撤单队列通常已有线程安全投递，不能把此反例说成已证明撤单必失败或持久文件损坏；验收检查策略状态真正只在 loop 变化。
- 两策略复用同签名窄 callback 和纯报告/cache/业务事实比较函数；不复制 Maker 的长比较器给 Taker。停止时使待完成回调失效，晚结果不能在已停止策略上解锁或发单；原 source/hedge 义务不清除。
- 共享任务在排队前同步关闭新源准入，覆盖两客户端 await、原生事件补齐、adapter 精确确认与逐订单回调；已知 hedge 不因此停掉。每轮整体 `wait_for` 覆盖核对和所需单笔报告，最多两轮加一次短退避。失败保留原因/业务 gate，不把 native 的部分应用回滚成未成交；下一轮只补尚缺的权威事实。
- 查询失败、超时或客户端单独取消根任务时，只要 Actor 仍运行，就保留尚未交付的原订单回调及策略 inflight；不提前回调 `None` 丢失后续恢复的接收者。同一失败订单的重复事件只合并，不重置两轮预算；显式恢复或新的待核对 episode 才重试。另一个订单成功不能抹去先前失败，Actor stop 才清除待回调集合。
- 单笔回调返回明确的确认结果：只有报告/cache/业务事实精确一致且确认持久化成功才算已交付。报告不一致或写盘失败保留原回调及策略 inflight，纳入同一两轮预算；显式恢复后仍能到达原等待者。运行时不得把“回调没有抛异常”当成业务确认成功，成功回调也不重复交付。
- MT5 当前原生 mass-status 路径在取消时会关闭执行连接；每轮前后检查两执行客户端连接。一次超时已导致断开时不继续第二轮，也不在本包新增自动重连；保留实际成交与恢复原因。
- Actor 停止取消所有自己创建且登记在 execution client 的根任务/等待任务；客户端 disconnect 继续用原生 `cancel_pending_tasks` 收束。一个等待者取消不能取消其他等待者共享的根任务；停止/断开则必须能取消根任务。canary 的全量对账需复用同一入口，不能旁路另发并发全量查询。
- canary 使用不重启失败动作的共享入口：已有根任务仍在首轮失败后的退避或第二轮时，加入该任务等到最终结果；已结束且失败时返回未完成，不重开预算，也不顺便恢复另一个失败订单。普通显式恢复入口仍可重新核对保留的请求。
- 本包不扩大 `_can_reconcile_silent_terminal` 的既有认证范围。无终态消息的普通非 reduce-only IOC、Maker 撤单缺 ack 等如果当前 adapter 不提供可核对事实，仍列未验收场景，不能因为新增 Actor 就声称所有 UNKNOWN 自动恢复。后续只根据具体反例补充同一适配器的窄认证路径，不重发旧单。

**W4b 普通 Maker 组合反例补充：** 实际 Bitfinex adapter + LiveEngine 补 `partial fill → canceled` 时，Maker 在 fill 回调排队的保护性 cancel 可能晚于 canceled/adapter retire 才执行，随后产生 cancel-rejected。它是撤单动作失败，不应把已经一致的订单终态回写成 UNKNOWN。仅当该策略拥有的业务记录与 native cache 都为同一个已确认形态的 CANCELED/EXPIRED/FILLED，且事件账户、instrument、client/venue order ID、数量/已成交量与持久路由一致时，保留既有事实和所有 HOLD，不因迟到 cancel-rejected 新建 UNKNOWN；不据此清 gate 或省略尚需的终态对账。工作中订单、身份不符、业务/native 不一致仍走原 HOLD。此修复用普通 Maker 两腿事件回归，不通过吞 adapter 拒绝或替身取消回调掩盖。

**W4c 静默终态补齐（2026-09-05，实施前固定）：** 当前代码的静默触发器只覆盖本进程提交的 Paper reduce-only IOC；普通 IOC 的 submit timeout 只有 UNKNOWN，已收到 accepted 的 IOC 又会撤销 submit ack timer，Maker cancel timeout 也没有终态查询触发。这是普通入口仍可能依赖人工恢复的具体缺口。本包仅沿用 W4b Actor 的原有两轮预算扩展以下当前测试市场路径，不新增重发、定时 REST 服务或自动重连：

- Paper 本进程 IOC（普通开仓和既有 reduce-only）从实际提交保留终态等待期限；accepted 仅确认接单，不能取消最终结果等待。期限内完整真实 fill 或终态到达沿用既有事件；到期仍缺最终事实时由现有 Actor 查询，无论 submit ack 是否先到。
- Paper 本进程 Maker GTC 只在已经确认接单且实际发起 cancel 后登记静默终态等待；正常工作的挂单不能仅因挂得久就被当成终态。非 submit/cancel 的未知修改、冷重启订单和真实市场静默恢复不在本包放宽范围。
- 明确的 Maker cancel 拒绝结束的是该次撤单动作及其静默等待，不代表订单成交或业务 HOLD 已恢复。后续显式新 cancel 使用自己的完整等待期限，不能继承旧动作的过期 deadline 而立即耗尽两轮查询；不会因此自动重新 cancel，也不能清 IOC 的提交终态期限或其它未知修改。
- 静默终态只能由 account-bound CID、精确账户/品种/client/venue order ID、side、quantity、limit price、TIF/flags，以及完整真实成交集合共同证明。非零成交的 liquidity、原订单类型/价格、成交时间区间、累计数量及均价须一致；不采用 inferred fill 替代缺失真实成交。零成交必须有明确 canceled/rejected 历史，历史为空或只返回 ACTIVE 不是终态证明。沿用原生 Position 数量交叉核对与原有 cache 精确确认后才 retire；查询期间不盲清 UNKNOWN。
- 原生表示差异须按实际接口核对，不把同义表示变成永久暂停：零成交 native `avg_px=0.0` 可与 REST `None` 对应；非零均价不放宽。另实际 Engine 探针与安装源码确认 `OrderRejected` 不携带 venue ID，未接单直接拒绝后 cache 的该字段仍为 `None`。仅允许本进程 Paper IOC、尚未 accepted、同一已核验 REST 零成交 REJECTED 与 cache 零成交 REJECTED、native 无任何 fill，且持久 CID 与 adapter 中唯一 venue 绑定均吻合时使用 REST 的 venue ID 完成 adapter 确认；不伪造 accepted 事件、不改 native cache 绑定。已存在但不匹配的 ID、CANCELED/FILLED 缺 ID、冷订单或任何成交仍必须失败，其他账户/品种/方向/价格/flags 等比较全部保留。
- 本包不新增 Paper 缺 post-only bit 的豁免，不把价格修改的含混历史当成证明。迟到相同 TU 不得重复 fill/hedge；冲突、少 trades、错身份或两轮无结果继续有原因地暂停，重复 timer/行情不重置预算。
- 两轮之间已有部分原生状态生效时，adapter 尚未 retire 的 `reconciled_terminal` 仍须核对本轮最新同 CID 报告及完整真实成交；不能因 native cache 已 closed 而跳过，继而拿上一轮报告确认成功。报告缺失或身份/数量/flags等变化本轮失败，保留第一轮真实成交和既有义务；不回滚、不补发。完全一致的第二轮应能完成，包括 native 缺 venue ID 的零成交 REJECTED；非零成交仍保留已有 cache fill 的身份/数量/价格/费用核验。
- D02 分类校正：当前 adapter 不支持 GTD/MTS_TIF，REST 状态映射没有 EXPIRED；IOC 到期撤余量映射为 CANCELED。因此用真实 `IOC CANCELED` 覆盖本协议的过期语义，保留策略对原生 EXPIRED 的防御测试，但不新增虚构 wire 状态，也不宣称 EXPIRED 后迟到 fill 已能自动恢复。
- 验收使用普通 Maker/Taker builder + LiveExecutionEngine、实际 Bitfinex adapter 与合成 venue IO：未 ACK/已 ACK IOC、已发 cancel Maker 的零/部分/全部成交，自动核对后实际对冲和下一次源单；不直接调用 runtime.reconcile 或修改 store 完成状态。MT5 wire/EA、完整启动/重启与 native 费用恢复仍分别由 W2/W6 验收。

**W4d 工作中 Maker 漏消息发现（2026-09-05，实施前固定）：** 主 agent 完整读并独立运行 `runtime/q6_open_order_probe.py`（SHA256 `feef376214f284f31b796dfa5211b3c173d74ce82dbf6f7842820118a18f2b48`）。实际 NT 1.231.0 证明：原生 open-only 不处理消失的挂单；返回仍 ACTIVE 的部分成交时又可生成 inferred fill，随后同笔真实 TU 造成重复 fill/hedge；非 open-only 在无历史时直接 REJECTED，partial canceled 可被误关成零。QueryOrder 也绕过共享任务。故两 builder 原生 open/inflight checker 继续关闭，不修改框架；使用现有 Actor 与原生报告 API 的只读部分。

- 现有 Actor 最多每5秒检查本进程 Paper、已 accepted 的 post-only GTC；没有符合范围的工作单时不查。只读取原生 `GenerateOrderStatusReports(open_only=True)` 对应的 active-list，不周期扫描两周历史，也不把返回报告发给 Engine 直接应用。检查本身与全量核对共用一个 client-owned root、同一 busy 门、超时/两轮预算和 stop/drain；不创建第二个轮询服务、timer或全量并发任务。
- 精确不变的 ACTIVE 只结束本次健康观察，之后可按间隔继续观察；单纯订单年龄不触发 cancel 或终态。订单消失、实际成交量变化或报告与本地事实冲突，仅表示需要共享全量核对，不代表 REJECTED、CANCELED 或已成交。查不到历史/少 trades/身份不符仍有界暂停；失败 episode 不能被每次timer/行情重新打开预算，明确事件或显式恢复沿既有入口处理。
- 普通组合新增反例固定短暂 busy 的含义：旧 Maker 把 `live_submission_ready=False` 同时当行情/执行故障，因而 active-list await 中一次正常行情就会撤销健康挂单。只为 Maker 增加窄的“暂缓报价刷新”回调；本次尚未发现差异的健康观察可保留其它全部健康判断，但不得 submit/modify。真实数据断开、陈旧成本、未知成交/修改、全量恢复和显式 stop 仍走原保护性撤单路径；不能因有查询在途就屏蔽这些风险事实。复用同一 Actor 的状态，不引入第二套 readiness 平台或全局无条件 busy 豁免。两组真实行情穿过 await 的阳性/故障对照必须验收。
- 只核对该次 await 前后仍属于同一 live/CID/venue/order 的候选；过程中正常 WS 填补、撤单、改单或 retire 时重新读取事实，不用旧 active 快照覆盖新成交/价格。pending/UNKNOWN modify 不扩大认证范围。观察旧价格和本地已确认新价格冲突时进入完整核对或保守暂停，不把它当作改单成功；现有 Paper post-only bit 的豁免不增加到成交/终态。
- full mass 对本范围的终态以及仍工作的部分成交，在交给原生 Engine 前都要求完整真实 trade 集合、数量/均价、liquidity、raw type/order price/time 与原订单吻合，不能因没有先发 cancel 就走 inferred。REST 补齐的精确终态须登记给既有 adapter confirm 后 retire；工作中真实 fill 通过原生事件进入原 Maker 义务/保护性撤单路径，不伪造终态。随后正常 WS 终态保留原 `terminal_emitted` 完成及已关闭订单的内存保留行为，不为测试要求全局更改退休策略。adapter 的已应用成交量也须与已证明的 native 真成交一致，避免 REST补一笔后新TU漏算累计或重复 hedge。
- 独立审核补充的完整集合反例：首轮已应用真实成交 ID A 的1oz，次轮报告累计2oz但 trades 仅有新 ID B 的2oz；即使本轮合计与报告相等，也必须在交 Engine 前拒绝。完整真实成交核验须覆盖已应用的每个真实 native trade ID 并保持其精确映射；不得先应用新成交/义务再靠 native 返回 False 发现累计3oz。失败保留已知1oz及原义务；合法 A=1oz、B=1oz 可继续一次性应用增量。此要求只放在已有的完整真实成交证明边界，不施加到只读 active-list 的空 fills，也不扩大 inferred/cold 认证范围。
- 查询次数按真实恢复动作计：同一发现失败后单纯timer不会重试；若随后真实行情触发原 Maker 对工作单的一次保护性 cancel，该新 wire 动作可使用原 W4c 的两轮终态核对。此组合最多“发现2轮 + 该次撤单2轮”，重复行情不重复cancel、不重复开启预算；不能为了让一条错误的 `attempts==2` 断言绿色而吞掉应有的保护性撤单。没有新wire动作/明确事件或显式恢复时仍不重开原失败预算。
- 单次查询中的 WS 竞争和两客户端首轮部分生效保留 W4b/c 规则：已发生的真实 fill/hedge 不回滚，下一轮核对最新事实；不能借一次正常 ACTIVE 清除未知修改、其它订单失败或原业务 HOLD。停止不交付晚结果，不发额外源单或撤单；终止未决状态留给 W6。
- 主 agent 用普通 Maker builder/策略与真实 LiveEngine，合成 venue IO，覆盖未发cancel的零/部分/全终态、仍ACTIVE部分成交后正常撤单、late TU与下一真实源单；负例覆盖 missing history/trades、错误type/price/time、正常ACTIVE持续保持、timeout/stop及WS竞争。不得通过测试手工确认store或显式调用reconcile替代自动发现；本包不证明真实市场、冷重启、MT5 wire或完整启动/停止已完成。

### 4.5 方向、仓位与净额

- LONG/SHORT 是源腿方向；Maker/Taker 是两种策略。相反 direction 不等价于两程序不能同时存在。
- `positions[]` 按 venue/account/instrument 限定，列表元素是 position 不是 order。MT5 用票据集合，Bitfinex 每 symbol 的 venue 净仓是单一投影。
- 目标是执行 signed delta，不是每次重设某个净仓目标：已有同向票据不吸收新增 delta；先关闭反向票，再开剩余；下一腿前重新读取当前票据。
- 比如当前 MT5 BUY 1oz + BUY 2oz，执行 SELL 4oz，应 close 1、close 2、open SELL 1；中间任何未决结果都不继续后续腿。
- Maker bid/ask 同 route 的已确认小额成交可以按净额抵消，但必须同一原子写单元记录抵消量和原始 fill 身份。旧两个 JSON 不能依次扣减后宣称原子；W5 使用一个 Maker 状态文件、两个方向 view，复用原协调器，不引入两阶段提交。W5c2 已通过 strict 同 route 净额验收（E04/E05），W5c3 已交付显式旧状态转换，W5c4b 已通过单Maker有界carry离线验收；共账户残差额度仍归W7。
- 只有相同账户/品种/route 的未分配残差可抵消；已经提交 hedge 的义务不可被另一个方向擦掉；净额不为零的 dust 保留并应用明确额度/暂停规则。

非零 dust 的交付规则也要闭环：实现 `strict` 与 `bounded-carry` 两种明确模式。strict 保持当前零残差才开启下一 cycle；bounded-carry 只允许相关源单终态已确认、无 UNKNOWN/在途义务时把小额残差带入下一轮，残差必须持久化并计入共享敞口预算。现有 1oz hedge 舍入步长下 carry 上限不得超过 0.5oz，也不得超过 profile 的残差限；±边界、累计越界和反向抵消均测试，不暗改舍入算法。

carry 是同 route 的唯一 signed residual；下一 fill 先与它合并再分配新义务，不能只放宽 gate 却保留旧方向残差再次分配。W7 的 Maker/Taker 共享同一 route 残差限和原子记录，不是每个策略各领0.5oz。准入还要计算 carry + 在途义务 + 源工作单最坏成交：例如 carry +0.5oz 再 BUY2oz，短时未对冲可能2.5oz，不能通过 max_unhedged=2oz 的 profile。

当前在线 profile 默认 residual limit=0，不因写此计划而增大。bounded-carry 先离线验证，W9 会话 profile 明确预算后才能在线启用。持非零残差停止应报告其数量和恢复条件，不宣称 FLAT；新的源订单是正常经济机会，不为消除 dust 无条件成交。

## 5. 实施工作包与依赖

主线：**W1/W2 → W3/W4 → W5 → W6 → W7 → W8 完整验收 → W9**。W8 的安装与文档检查可前移；W3 的纯经济值测试可与 W1/W2 并行。依赖是行为集成依赖，不是要求串行等待所有不相关文档。

### W1：统一成交费用语义

范围：`bitfinex_v1_execution.py`、`bitfinex_v1_reports.py`、`bitfinex_v1_cids.py`、`mt5_v1_execution.py`、一个候选短费用转换模块及对应测试。协议仅澄清 native commission，不反转 EA wire。

实施步骤：

1. 把审计反例变成失败测试；修正当前要求小数分必报错、负佣金即正确的测试预期。
2. Decimal 费用转换共享；同样的 raw fee 在 WS/REST/重启生成同样的 native fee。
3. TE 一次成交，TU/REST final fee 幂等补充；execution facts 变化仍报错，不能把任何 commission mismatch 全局忽略。
4. 费用写入失败保留已知成交，显式 accounting incomplete；不能写成阻断对应 hedge 的全局 execution_hold。REST 可重建，不产生第二次 hedge。
5. 最终费用/PnL 报告标识 provisional/final 与币种；缺 final fee 不伪造零费通过。

验收：A01–A08。推荐拆成“转换正确性”和“Paper 最终费用”两个可独立审阅提交；第二部分不能拖延第一部分修复。暂停此包的唯一技术前置是 Q1 中明确列出的原生探针，不顺带升级 Nautilus。

### W2：EA 原生事实与错误分类

范围：`mt5_ea/include/Py000Protocol.mqh`、`Py000Execution.mqh`、`Py000Journal.mqh`、必要的主文件调用点；Python MT5 codec/execution 和对应测试；协议/source-manifest 测试。

实施步骤：

1. 完整 snapshot builder 对读取失败返回失败；按 4.2 固定两轮采样并比较稳定字段，不发半列表；Python 将暂不可用与破损/身份变化分开处理，后续正常采样能恢复。
2. 抽一个短 retcode 分类函数。首批验证 REJECT、MARKET_CLOSED、NO_MONEY、REQUOTE、PRICE_CHANGED、PRICE_OFF；判定拒绝同时要求无矛盾 order/deal/filled volume。其它码逐项用契约/测试加入，不笼统把 !sent 当 rejected。无法证明的码仍UNKNOWN并列出尚不支持，不能由三个示例通过代表所有正常拒单完成。
3. DONE 仍检查实际 deal 和精确数量；partial/timeout/connection 等保留 UNKNOWN；旧 journal 的 UNKNOWN 不按新白名单回溯清除。
4. 检查 seek、完整写入、flush 错误；验证实际编码/换行后的字节数，不能用字符数冒充 bytes。[FileWriteString 契约](https://www.mql5.com/en/docs/files/filewritestring)
5. 使用同一实际 MQL helper 的测试入口注入返回值；MetaEditor 编译及 DEMO 原生检查，source-string 检查只用于静态契约。

验收：B01–B08。协议若增加字段，先改协议文档再改 codec/EA；若只是内部错误分类和检查，不为了形式升级 wire。新 EA hash 正常重新计算，不手工绕过验证。

### W3：行情触发与动态成本

范围：`strategies/maker.py`、`strategies/taker.py`、`mt5_v1_data.py`、相关成本纯函数与事件测试；必要的 Instrument 订阅 wiring。两策略相同的 Instrument 结构/swap 解析可抽入私有 `strategies/_mt5_costs.py`，仅纯计算、无 I/O 或新生命周期。

实施步骤：

1. Maker freshness 与经济值变化分支独立；同值新时刻不撤单，同时 stale timer 正确续期。
2. Taker 抽取一次 `_evaluate_and_submit` 类私有方法供 source book 与 MT5 quote 调用；不复制经济函数；防同轮双事件重复提交。
3. MT5 dynamic swap 与 immutable structure 分开比较，通过原生 Instrument 更新传播。
4. 策略目前缓存 `_hedge_instrument` 引用，必须订阅/处理原生 Instrument 更新或显式重新取 cache；只替换 cache 对象不够。
5. bid/ask spread、carry 正负、三倍 rollover、Athens 日期边界及成本过期均重跑对照。

验收：C01–C07；不用 canary 价格 override，不放宽真实 freshness 以通过测试。

### W4：把终态恢复放回普通运行路径

范围：两 live builder、两策略、`store.py`、Bitfinex execution 的既有报告接口；候选 `live_runtime.py`；现有 canary 调整为复用。与 W1 修改同一 execution 文件时按提交顺序合流，不能双 agent 同时写。

实施步骤：

1. 复用 Maker 的窄 source terminal query 设计给 Taker 绑定同样能力；查询结果核对账户、order/trade、side、qty、price、终态和实际 fill。
2. 将 canary 独有 adapter terminal reconciliation 调用移至 node 生命周期内的受管有界任务；启动/停止、去重和取消都有归属。
3. 查询超时、暂时无报告和事实冲突分开记录；缺结果不释放，明确终态核对成功只释放相应 source。
4. late fill 正常经原生事件进入义务；partial 的剩余 hedge 完成后可再评估下一机会。
5. 公共数据暂时不可用只暂停新的源风险；既有真实成交对应 hedge 在其执行前提满足时继续；不将所有 HOLD 简单清零。
6. 保留 adapter 自己定义的安全错误原因和事件类型，不记录原始私有消息/凭据；普通日志能说明“为什么停、缺什么事实、可否恢复”。

验收：D01–D08。测试必须启动普通 composition，不通过 canary 中的特制 polling 才得到绿色。

### W5：策略行为 parity、容量与双向残差

范围：`economics.py`、`maker_economics.py`、`config.py`、两策略、`hedge.py`、`models.py`、`store.py` 及真实 BacktestEngine 集成测试。

实施步骤：

1. 对原 oracle 使用脱敏输入/输出向量，列出严格阈值、short-first、价格、carry/FX、动态 margin、route 选择的规则；对未认证原 callee 单独列“未认证”，不生成假等价结论。
2. 从原生账户 equity/margin/free、instrument 及原策略公式计算动态 capacity；新增风险取动态 capacity、配置硬上限、方向性净仓空间、可执行数量的交集。保留原规则的降风险例外：已有12oz、上限10oz时仍可减到11oz，不能用负的剩余空间拒绝减仓；穿零的新开部分另计容量。不可用账户数据不假造无限额度。
3. 容量包含未成交源单最坏敞口与未完成义务；Maker 双边 reserve 的确定性规则用测试固定，不把净额抵消当两边永远同时成交。
4. Maker 两方向状态合为一个原子存储，方向 view 保留；同 route 残差抵消保留可追踪的 fill 分配，不在跨文件之间“先扣一边再扣另一边”。
5. 实现 4.5 的 strict/bounded-carry 规则，零净额和有界非零残差分别验收；不以非零残差永远停住来声称任意部分成交后的连续性已完成。
6. 运行连续 LONG→LONG→SHORT、部分反向、跨零反向、多票关闭；每步都检查 source/hedge delta，不强制每步 flat。

验收：E01–E09。状态迁移按第 7 节。跨策略同时写账户放到 W7，不用扩大本包来造统一风险引擎。

**W5a 原容量纯函数（2026-09-05，实施前固定）：** 先交付已认证 callee 的 Decimal 计算及脱敏固定向量，不接账户、策略、定时器或 live profile。只新增 `margin.py`、对应测试及一份紧凑 fixture；不导入原脚本，不让 CI 依赖本机 ZIP 或旧项目。

- 输入是已规范化且全部显式提供的数值，输出一律为物理 `(BUY capacity, SELL capacity)`。Bitfinex 的 `L=floor(10000/(target+base))`，`H=min(floor((collateral+PL+available)*L/price), floor(free*L/price)+abs(q))`；MT5 的 `L=10000/(target+base)`、`H=floor(equity*L/ask)`。两者返回 `max(H-q,0), max(H+q,0)`；不复用会把杠杆抬到1的下单 `expected_leverage`。保留 Decimal 净仓，不复制原账户生产者的 `round(q)`。
- 原脚本的 `duration` 是返回顺序映射，不是物理仓位符号。fixture 对照时按原 `duration` 换序；当前 hedge 的 `max_long_ounces` 是 MT5 BUY，不能把原 `duration=-1` 的 tuple[0]（SELL）接成 BUY。已有仓位的基础 margin/entry price 保留原式；MT5 live 接线仍使用原固定 base=60，不以账户 leverage 偷换。
- 所有参与值必须是有限 Decimal，price 与 target+base 为正；缺失不是零容量、更不是无限容量，非法输入显式失败。容量是该公式的结果，不是最终发单许可；配置上限、降风险例外、工作单/义务占用在后续接线包处理，不在此凭一个函数宣称可连续交易。
- flat 的迁移政策固定为：只有完整账户/持仓样本证明该 route 精确零仓时，才允许使用显式正的 route `base_margin_level` 与 fresh/actionable Bitfinex ask，零 collateral/PL 必须来自已证实无仓；非零仓字段缺失不得进入 fallback。当前 route 默认0及任何 profile 不在本包修改。W5a 只验证显式 normalized flat 输入；样本完整性、资金/行情时效和 flat 判定的实际接线留给 W5b，不把纯函数包装成账户认证。
- 固定向量记录 ZIP、成员、选定方法及原向量哈希；2,000 normalized 向量与两端原输出按 `1e-9` 绝对容差对照，另测试 Decimal 取整边界、整数/非整数杠杆、零杠杆、精确小数净仓与物理方向。normalized flat 的 `(5,5)` 明确属于上述迁移入口；原 Bitfinex flat producer 的除零缺陷仍单独记录，不能声称原账户生产链全等。
- 独立数值复核补充：MT5 的 `H` 用等价的 `floor(equity*10000/((target+base)*ask))` 直接求值，不先把重复小数的 leverage 舍入后再乘。`target=400, base=60, equity=184, ask=4000, q=0` 的原 callee 与精确代数均为 `(1,1)`；初稿中间舍入误成 `(0,0)`，属于必须修正的迁移错误。增加该现实量级整界及两侧，不以随机向量绿色替代边界验证。

**W5b 实施切口（2026-09-05，只读勘查后固定）：** 分为“既有账户事实入口”与“策略视图/占用接线”两个可验证小步；W5b1的实际结果单独记录，后续接线尚未完成。沿执行客户端 → 原生 `AccountState.info` → `margin.py` → 现有账户视图，不引入风险服务、另一份义务账本或独立周期轮询器。

- 第一步主要写集为 `bitfinex_v1_protocol.py`、两个现有execution及其测试。BFX补齐现有PositionState的collateral/collateral_min等容量字段，处理现在被忽略的`ps/pn/pu/pc`，复用完整REST positions读取；严格限定配置的账户/品种。保留钱包available的原始缺失状态及钱包、持仓各自观察时刻/完整样本事实；`auth+wallet`、`get_account()!=None`、native净仓0均不单独证明flat。完整元数据随AccountState发布，不能由新钱包事件覆盖或刷新旧持仓样本。
- 主agent重新核验原ZIP及BFX成员SHA并只读原callee：真实producer的`freeMargin`和容量式的available都来自同一个`wallet.available_balance`；有仓`B=10000*collateral_min/(collateral*leverage)`，容量价格分母另为`position.base_price`。normalized fixture中F/A独立采样不代表真实接口独立。迁移保留精确Decimal净量，但不复制原`available=None→0`或flat除零；有仓B还须送入现有SourceAccount，避免容量用动态B、下单杠杆却用静态B。
- MT5复用已有完整snapshot、raw equity/margin/free及1秒刷新，传递同一完整positions样本的精确净量。execution账户时间需独立检查年龄/未来/倒序，不能由data的新鲜行情替旧账户样本背书；B仍固定原值60。暂不可用只影响新的源风险，不阻断已知真实成交的原对冲路径。
- 持久无账户变化也应可继续：不能只给字段加TTL后被动等永远不来的更新。需要刷新时优先实现已有native `QueryAccount`入口并复用现有wallets/positions接口，按需、有界、去重、归现有客户端生命周期，不额外起周期服务。钱包/持仓读取间发生真实fill或连接更换时，不发布混合样本；净量相同但中间开平过仓也不能使旧样本重新有效。具体时序先写反例再接线，不为此改Nautilus缓存/框架。
- 第二步才改两策略账户视图与必要的短共享纯映射：动态容量、route配置硬上限、方向净仓空间、实际可执行量取交集；沿原降风险例外，已仓12/上限10仍可减到11。MT5物理BUY/SELL不按旧duration换错；缺账户容量不假造无限额度。纯公式完成不等于这一步完成。
- 占用从现有SourceOrderRecord、native leaves与store.intents读取。BUY/SELL各算最坏剩余，相反挂单不提前抵消；重新评估旧报价排除它自身，pending cancel未确认前不释放，partial的已成交量由仓位/义务承接。不得猜测venue的available是否已扣挂单资金，继而把同一单从现金和转换后容量重复扣除；先区分资金占用与方向性最坏敞口，按当前接口证据在本节补齐精确规则，再实施占用接线。W5c原子残差与W7共策略lane不混进本小步。
- 最小验收包括：wallet先到但无完整positions、合法flat/有仓缺字段、钱包与持仓时间互不刷新、采样间fill与净量往返、MT5旧/未来/倒序账户、物理方向、降风险与穿零、Maker双边/自身刷新/pending cancel/partial占用，以及margin不可用时原hedge仍推进。全部先离线，真实账户/profile/订单不在该小步内修改。

**W5b1 账户事实入口（实施前固定）：** 先交付元数据生产和时间资格，不修改策略、原execution admission、钱包Money表示、未知订单处置或既有hedge流程。QueryAccount按需刷新、实际动态容量/杠杆及挂单占用接线属于W5b后续，不据本小步宣称长期连续准入已完成。

- BFX保留`bitfinex_wallet_currency`，在`AccountState.info`新增单个`bitfinex_margin`字典：`instrument_id`；`wallet={balance,available_balance,observed_ns,current}`；`positions={complete,current,observed_ns,position}`。position为null或单个对象，含`position_id/status/quantity/base_price/profit_loss/leverage/type/collateral/collateral_min/venue_update_ms`。Decimal值用字符串，缺失保留null；无完整样本时position=null不表示flat。`current`只表示本客户端未观察到失效事实，不等于年龄合格、字段齐备或允许下单。
- 复用现有protocol/REST报告映射，字段依据[Bitfinex WS positions](https://docs.bitfinex.com/reference/ws-auth-positions)及[REST positions](https://docs.bitfinex.com/reference/rest-auth-positions)：17/18为collateral及最小collateral，13为venue最后变更时间，不能拿它替代本次读取时间。完整PS/REST可建立单一配置symbol的完整投影；局部PN/PU不能在此之前创造完整性。PC须匹配已知position身份及明确关闭事实；迟到旧ID/时间、重复冲突或含混目标行不能覆盖last-good或伪造flat。独立反例补充：含混PS、REST报告映射拒绝/enrichment解析失败或被拒的目标持仓局部事件均撤销complete/current，保留last-good对象和时间；后续PN/PU/PC不能恢复完整性，只能由合法完整PS/REST恢复，不能用“关掉已知票”排除另一张未解释的票。
- 钱包/持仓各自更新观察时间。新增margin字段坏值或null只令容量元数据不可用，不使reader fatal；原核心身份/单位校验不放宽。新的已应用native fill使此前两部分current失效，重复/费用补充不伪造新变化；不要求先有fee CID binding。连接更换/断开使旧样本失效。REST await遇新持仓、真实fill或断线，不发布旧结果为当前元数据；超时/取消不刷新样本，取消保持传播。原报告执行结果仍按原接口处理。
- MT5保留现有六个raw账户字段，追加`mt5_positions_complete`、`mt5_net_position_ounces`、`mt5_position_count`、`mt5_symbol`、`mt5_stream_id`、`mt5_account_observed_ns`、`mt5_account_sample_valid`。净量为同次完整票据的`sum(±Decimal(volume_lots)*Decimal(contract_size))`，保留票数以区分净0与无票；foreign magic的原HOLD不清除。时间来自该execution snapshot，与data行情和发布日期分开。
- MT5只新增窄的`account_capacity_ready(max_age_ns)`读取资格，调用方后续传入已有`max_cost_age_ns`；以本客户端当前clock实时判`0<=now-observed<=max_age`并结合现有连接/完整样本条件，不新增配置、timer或全局execution gate。future/严格倒序令本次容量样本无资格，不扩大原执行路径的断线/异常分类；仅合法样本推进时间水位。EA时间为秒精度，所以相同时间的合法账户内容变化可以发布。新的真实fill令旧容量样本失效，原完整刷新后恢复；不妨碍fill事件传播及原对冲流程。
- 独立安装版探针已证明AccountState.info不自动合并、可别名共享且原生允许倒序。两端每次发布均构造新的完整字典及嵌套对象，不能原地改历史事件；元数据资格读取必须针对同一份完整事件，不用新钱包或data事件更新其它组件的时间。
- 写集限BFX protocol/execution与其两测试、MT5 execution与其测试；主agent维护本文及必要的普通composition集成测试，未参与本包编码的reviewer独立复核。最小RED矩阵固定auth+wallet未有PS、合法flat/有仓nullable、局部/迟到/错ID、时间互不刷新、REST await竞争/取消、native fill与重复、MT5精确净量/票数/age边界/未来/倒序/同秒更新及历史info不被修改。验证后作为独立本地提交。

**W5b2 按需账户刷新（实施前固定）：** 只补Bitfinex现有native `QueryAccount`入口；MT5已有250ms事件轮询和1秒完整snapshot/journal核对，沿用原路径，不为接口齐全性增加第二种强刷。两策略、runtime、动态容量/杠杆、挂单占用、profile和EA均不在本包修改。

- 本机1.231.0已核验`ExecutionEngine → client.query_account`路由，公共命令返回None；成功通过原生AccountState/Portfolio交付，异步错误沿现有client task日志交付，不虚构命令应答。入口精确检查account、可选client ID及已连接/已认证运行条件，不满足时仅日志并返回，不向原生同步命令路由抛异常；一个client-owned、强引用的在途task复用原`create_task`，重复查询不再创建I/O或排队重试。
- 顺序读取已有`rest.wallets()`与`rest.positions()`，整个采样含锁等待共用既有`rest_timeout_secs`预算，无新配置/定时器。钱包与持仓分别记各自read完成的观察时刻；两者全部验证和Money构造成功后，无await地一次安装、一次发布完整info，不中间发布新钱包配旧持仓。这里的联合采样只保证本地单次发布及没有观察到竞争，不宣称venue两REST接口提供同一时点事务快照。
- 保留W5b1的`_margin_revision`；另用一个目标钱包观察水位记录WS/WU（同值和同时间也推进）。公共入口排task之前捕获两水位，协程开始及各await后核对，不能把尚未开始的旧命令带到重连后的新连接。采样期间目标钱包/持仓观察、真实已应用fill、开平往返或连接变化均丢弃候选；不以净量相同作为未发生交易的证明。原独立positions REST仍只依赖持仓水位，不能被无关钱包刷新错误打断。
- 复用现有wallet协议、position报告mapper及转移判定，不复制第二套validator。目标钱包缺失/重复不补零；available=null及辅助margin字段null保持其原语义。无效候选不能安装半份样本；仅当原水位仍适用时撤销对应旧资格，含混持仓按W5b1撤complete/current并保留last-good对象和时间。迟到成功或失败不能覆盖更晚事实。普通超时/取消不刷新观察时间，取消继续传播，只有后续显式新查询才重新采样。
- disconnect与reader故障先让旧水位失效，再取消并await在途账户task；不能等到基类最终cleanup才处理，也不能在旧任务finally中清掉新任务引用。覆盖排队未启动/读到一半/重连等时点。账户查询失败不新增execution HOLD、不清UNKNOWN、不阻断已知fill的原hedge路径，也不触发任何订单操作。
- 写集：BFX execution与其测试；为新增REST protocol方法只允许补`test_bitfinex_v1_engine.py`已有FakeRest的同名只读方法。主agent维护本文和`test_strategy_continuity.py`的真实native Engine/Account/Portfolio集成。最小RED矩阵为无推送时显式刷新、并发去重、错误身份零I/O、无中间混样本、两独立时间、wallet/PS/fill/往返竞争、nullable/缺失/重复/坏样本、预算/取消/后续恢复、断线/fatal/重连旧任务收束。通过独立审核及全量后单独本地提交，不混入后续策略接线。

**W5b3 账户事实到动态容量视图（实施前固定）：** 在既有`margin.py`中一次完成两个venue的纯映射，输出既有`SourceAccount`、`HedgeAccount`、`MakerAccount`，不用新账户模型/服务/状态文件。先由普通组合的真实AccountState调用映射并送入原经济函数验证；本包不改变策略运行准入。自动视图、原生query触发/节流与Maker自身/双边/partial/pending-cancel占用在下一个接线包一并处理，避免可用余额已反映挂单时引入自撤循环。不得据本包声称长期连续动态准入已完成。

- 同一份native AccountState包含预期account及品种/币种或symbol/stream身份；只读所需事实，不混用portfolio净仓和另一事件的元数据。返回既有record或None；缺失、错误、过期/未来及无法计算的样本不得变成零仓、静态额度或无限容量，也不修改账户或历史info。当前客户端读取资格必须显式传入、无默认True：尤其MT5真实fill会撤adapter内部资格而不改历史AccountState.info，历史valid本身不足以证明当前可用。该布尔事实与最新event须由后续调用方在同一同步读取中取得；映射不替代现有连接/UNKNOWN/foreign-magic执行检查。
- BFX分别核对wallet和完整positions的current及观察年龄，positions的position键必须存在，不能把缺键当null/flat。有仓采用同一记录的精确q、base_price、collateral、profit_loss、leverage和collateral_min，计算`B=10000*collateral_min/(collateral*leverage)`；collateral/leverage须正、collateral_min非负，所需数值有限，available原始null不补零。free与available都取同一wallet.available。完整合法flat才允许显式正route.base与fresh/actionable源ask，collateral/PL零来自明确无仓；有仓缺字段不退回flat入口。SourceAccount带回动态B，供原expected_leverage使用。
- MT5核对独立账户观察年龄、complete/sample_valid、当前客户端资格、预期account/symbol/stream及精确净量；校验票数与净量的一致性，允许多票净0但不将其宣称无票。容量复用raw equity、fresh/actionable MT5 ask及原固定base60，不用账户leverage替换base。Maker和Taker复用同一MT5映射，不复制两套字段校验。
- 现有record与route的max_long/max_short均为本次物理BUY/SELL可交易量，不是持仓上限。最终两方向分别取原动态容量、route方向容量及`max(M-q,0)`/`max(M+q,0)`的交集，M来自对应RiskConfig绝对净仓限额；不从已含q的动态容量或route容量再减q。保留12/限10减至11和跨至-10，阻止跨至-11或-12；相反方向镜像、精确小数与零杠杆均验证。MT5方向不按旧duration倒置。可执行数量、挂单/义务占用仍属后续实际准入，不用本映射假装已处理。
- 写集限`margin.py`、`test_margin.py`，主agent维护本文和必要的`test_strategy_continuity.py`。先RED再实现：完整flat/有仓动态B、nullable/错身份/各组件独立年龄、MT5真实fill后旧event仍valid但当前资格false、正反向/超限降风险/穿零及两策略实际经济产出。独立复核及全量通过后单独本地提交；不修改两策略、builder、runtime、adapter、profile、EA，不推送/部署/访问真实账户或发单。

**2026-09-06 / W5b3验证中的独立test-only前置（修正前记录）：** 首次全量1751过/1失败，失败为未修改的`test_maker_events.py`原生时钟线程测试要求handler恰好一次。对原测试做40次有界诊断，只记录原`_inputs_are_fresh`的实参/结果，第25次复现两次回调：相对deadline分别为-520001ns（仍fresh）和+843999ns（过期）。现有生产按设计先重排再冻结/撤单，不是线程错误。只新增这一测试文件为前置写路径：允许一次或多次同loop handler，仍严查非空、线程归属、恰好两次持久化、每张单各撤一次及最终冻结状态/0.5秒等待上限；不改生产时钟或策略。独立复核后先将此测试修正单独本地提交，再完成W5b3的全量和提交。

**W5b4 普通策略自动容量接线（2026-09-06，实施前固定）：** 正式两builder必须绑定同一短账户reader，普通策略消费W5b3映射，不新增live开关、账户模型、轮询器或资金预留账。未绑定的原构造方式仅保留现有离线/Backtest输入；不能据其静态容量声称live准入。本包保留单节点、单source/hedge账户、Maker每方向最多一单、未决义务冻结新源的现有限制，不混入W5c残差或W7共策略额度。

- 已重新核验原ZIP和`arbi_trader.py`既有SHA：`pick_candidate_trader`新route选择才调用`max_volume_for_margin_target`；已有Maker单经`get_limit_order_params`、`_update_maker_order`重算经济price/base margin→lev，不重新申请全部旧数量。[官方wallet说明](https://docs.bitfinex.com/reference/ws-auth-wallets)把available定义为未被活跃订单/持仓/资金占用的余额，[derivatives API](https://docs.bitfinex.com/docs/derivatives)复用该钱包接口。每单实际锁资和ACK/WU原子顺序未获保证，不能用`quantity*price/leverage`猜测加回，更不能把`_working_quotes`的desired字段当venue已确认杠杆。
- 新source必须动态容量、route交易量、方向净仓空间及实际可执行量全部通过。已有Maker单只在数量不增加的price-only范围维护：仍要求当前完整新鲜账户、动态B、route/方向净风险及MT5动态对冲容量；source不重新扣整笔旧free额度，route/risk须独立重算，不以`max(已截断动态容量,旧量)`绕过硬限制。price+lev改变所需额外抵押由venue最终accept/reject裁决，本地不宣称已精算；既有modify rejection/UNKNOWN→冻结及核对路径保持。
- 占用复用两store的active记录与native实际leaves。每方向只有一单，重估按精确CID排除自身，BUY/SELL最坏方向分别判，绝不靠双边净额相抵开放额度；不从已合并资金/route/risk的容量再次机械减全部订单。pending update/cancel、未核对终态不放新单；确认后历史CANCELED即使leaves非零也不占用。partial真实成交继续冻结两方向，已有intent/residual门保持，已成交量与native hedge leaves不重复计入未决义务。没有新理由在partial未完成期间开放报价。
- BFX在既有client内增加窄订单action水位和最近完整query对应水位，不持久化第二套账。发/改/撤及其明确确认、拒绝或终态使旧新增预算资格失效；账户query须在已稳定的操作状态开始，并在读取和发布之间核对action、wallet、positions水位。ACK前开始、ACK后才返回的旧钱包即使observed_ns较新也不得放行。单纯WS时间戳不能证明操作后的联合采样。水位只管新增预算，不破坏既有账户事实或对冲执行；取消/重连沿原task生命周期。实际capture点以barrier反例确定，不凭返回时间替代因果。
- 同一行情先提交的一侧同步进入native cache，后一新侧必须重新检查native未决状态和当前预算，不能复用回调开始时的空订单/资金快照。共同的时效/成本/义务保护先运行，再处理有需求但尚无单的方向和旧单维护；若新侧正等待有界账户刷新，暂缓健康旧单改价以免每个tick的modify持续废弃查询、饿死新侧，但当次回调不能跳过旧单自身风险撤单。数量为零的禁用方向不制造刷新需求。顺序复核明确：没有已在途操作时，合格的降风险SHORT与已不合格旧LONG的撤单可同一回调排队，不额外要求前者等待后者ACK；每向最多一单、物理方向容量各自通过，不能靠相抵证明容量。已有pending或真实fill/义务时的新源阻挡不变。
- reader在当前loop同步取得同份最新AccountState及当前client资格，使用原行情各自时间戳、已有max_cost_age_ns和原配置路线；MT5从现有account_capacity_ready读真实fill后的资格。BFX缺事实、需post-action预算或接近账户TTL时，仅按需调用原生QueryAccount，复用client单task和超时；至少2秒的发起间隔先记后发，失败不在同一事件栈递归重试。后续市场/既有MT5完整账户事件继续消费需求，不新建周期器；不把无账户容量卡在触发query之前。
- 原生Portfolio先更新cache再发布账户topic，而BFX失效账户通知可嵌套在策略处理真实fill之前。两策略显式订阅自身两个账户topic，handler只合并投递到已有event loop，不能同步发单或抛错打断fill→义务→hedge；停止取消待回调/退订，原generation隔离重启。延后一次读取最新事实，再复用原机会入口；Taker不伪造行情时间或重置market_ts去重。Maker账户最早过期时间并入原stale timer，不能因行情继续新鲜而保留过期账户报价。
- 普通REST补成交回归补充（修正前固定）：账户回调可先发保护撤单，随后native部分成交会把当前status从PENDING_CANCEL变回PARTIALLY_FILLED，但这不是撤单已结束。Maker从同一native order.events读取最近撤单Pending/Rejected事实；部分成交不释放仍在途的撤单，明确拒绝或终态沿原处理。不新增撤单台账、不按错误字符串吞拒绝；必须证明每单只发一次保护cancel、真实fill/hedge不丢、明确拒绝仍HOLD及终态后恢复。
- 写集：`margin.py`只加reader类型约定；BFX execution的窄预算水位；既有live_runtime的共享绑定、两builder及两策略。测试限既有BFX execution、Maker/Taker events、live builder/runtime和strategy_continuity，另`test_taker_cost_input`仅给绕过构造器的单时钟fixture补未绑定reader字段，主agent维护本文。先做真实RED：post-action查询barrier、冷启动无PS主动刷新、账户恢复不需新行情/不重复claim、旧单available下降不自撤、真实risk/MT5容量下降撤单、持续行情双边不饥饿、pending/partial/拒绝不释放、账户TTL与停止代际。实际普通双客户端路径必须覆盖，不以手动调用mapper或canary特制逻辑替代；独立复核、全量通过后再本地提交。不改profile/EA、不访问真实账户、不推送、部署或发单。

**W5b5 连续仓位与提交前票据一致性（2026-09-06，实施前固定）：** 承接E01–E03，先补连续成交证据，再修复证据确认的执行缝隙；不合入W5c原子残差、W6恢复或W7共策略lane。普通策略不得每成交一次就人为清仓；同向是追加delta，反向先减旧票，穿零才新开余量。

- 使用本机固定版本的原生BacktestEngine连续运行普通Maker/Taker，配置在启动前确定。通过真实行情/盘口触发原策略和撮合，不直接调用`_submit_source`、伪造store成交或回填native仓位。逐步断言LONG2→LONG2→SHORT2的净仓2→4→2，以及部分反向、穿零、多票严格close→close→open；同时检查实际订单数量、reduce_only、PositionId归属和已完成义务，不只看最终净仓。
- 实时提交边界另经原MT5 adapter及已有fake transport验证。当前候选反例是：planner读cache绑定close1→open1，但提交强刷发现同票已为2；以及close之后新快照仍有反向票。先保留RED证据，再将已绑定腿的最小前提沿原生SubmitOrder.params传到adapter，在其现有锁内、mutation前取得新完整snapshot并核验。缓存不得覆盖新venue事实，失败走原OrderDenied/义务未完成路径，不能继续余腿或重规划未知单。
- 只对明确带计划前提的策略hedge命令实施上述计划一致性检查；不把adapter改成规划器、不新增账户服务/状态账、不改变EA wire协议。现有exact-close ID/方向/手数和EA二次校验保持。新快照与EA实际发单之间的竞态不能据离线测试宣称消失；跨策略写入串行仍属于W7。
- 缺失/变化票据、同ID数量/方向变化、residual前新反向票、前一腿未决或拒绝均不得多发。fresh snapshot超时/不可用不能使用旧snapshot继续。普通两策略到真实adapter的参数路由必须独立验证，不以单独直接调用adapter或原append-only MT5替身冒充串联验收。
- 有界写集：现有hedge.py（仅必要的短命令前提映射）、两策略hedge提交、mt5_v1_execution.py；复用现有相关测试夹具，允许一个连续仓位矩阵测试文件。root维护本文与原生连续撮合测试，编码agent负责执行边界与对应测试，独立reviewer审核。全量离线通过后独立本地提交；不改profile/EA、不开真实账户、不推送/部署/发单。未覆盖的完整live连续矩阵继续明确列为未完，不以分层测试声称DEMO已验收。

**W5b6 双adapter连续成交闭环（2026-09-06，实施前固定）：** 从`dfa90fb`出发，先作为test-only包补齐E01–E03的普通live composition离线证据。沿原builder、两策略、两个实际execution adapter、原生Engine与原source-terminal reconciler运行；仅venue执行IO与行情事件为确定性离线输入，行情经原生DataEngine交付，不认证实际PUB网络或data adapter解码。不得覆盖adapter提交/成交/报告方法、直接写业务store或native仓位来完成周期，不放宽2秒账户query节流、准入、时效或原UNKNOWN处置。

- 现MT5 fake的pages是一次性响应队列，历史重读只返回stream_started，positions也不随mutation变化；这不够证明连续对账。仅在tests新增短的连续wire夹具，复用现有协议向量构造器：唯一request/order/deal/position ID、同一可重读有界journal、每次open/部分close/exact-close后的完整positions与新snapshot。snapshot、event分页和原generate_mass_status必须来自这些相同事实，不能分别拼出互不一致的绿色。它不是新EA或通用交易模拟平台，不启动ZMQ服务、不连外部账户。
- Bitfinex复用已有fake WS/REST和行构造器，按真实发出的on/ou/oc维护活跃单/终态/成交；trade与order ID唯一、SELL数量保留负号，单symbol净仓累计并正确经过零。账户查询与报告只读同一venue事实；原阈值/方向通过新行情驱动，不调用策略私有下单入口或重写market去重时间。Maker使用启动即确定的双边配置，选择实际已确认工作单模拟成交，保留原保护撤单与终态核对。部分成交可以小于已发单量，但不能超过真实leaves。
- 同一实例从flat连续经历2→4→2→1→反向1，以及1→3→反向1（反向delta4必须close1、close2、open1）；Maker/Taker及正反方向对称。每阶段检查实际source成交而非委托量、两端native净仓、MT5精确票据/剩余量、原adapter报告、store义务与下一源单准入；不同cycle不能用清仓、重启或手动解锁连接。MT5原0.02lot单腿上限保持，source4oz可由多个合格腿完成，不把整体delta误判为一张4oz MT5单。
- 增加腿间有界故障：前腿等待响应时不发后腿，后续票据变化/结果未知时停止余腿与新source。实测原MT5 mass在UNKNOWN时返回None、保留pending，原生对账不能宣布成功；不承诺必然立即断线，也不将None当空仓。不把旧synthetic mass的宽松行为当成真实adapter承诺。完成路径要对齐source部分成交、保护cancel及原生对账，不能只验证最后净仓相等。
- 写集：tests内一个短MT5 wire helper及其有限自检，一个双adapter连续矩阵测试文件；原test_strategy_continuity夹具只补必要的启动配置/IO注入选择，旧用例默认语义保持。root维护本文、BFX序列和集成矩阵；编码agent负责独立MT5 helper及其自检，不同时写同文件；另一个reviewer独立验收。若实际链路发现生产问题，先记录精确反例及最小修复方案再扩该处写集，不顺势重构平台。全量/静态/独立审核通过后本地提交，不推送、改EA/profile、部署或真实发单；原子残差、W6–W9仍是后续，离线闭环不等于DEMO验收。

**W5b6 窄修复补充（反例确认后、生产修改前）：** 首轮8案普通双adapter矩阵5过3失败。Maker源单真实事件依次为Accepted→PendingUpdate→Filled→ModifyRejected（原生Engine拒绝已排队的改价，原因为order already closed）；新行情和真实WS fill交错即可触发，未覆盖adapter方法。native源单已FILLED、MT5多腿已COMPLETED、原terminal owner无失败，但Maker无条件将该源记录降为UNKNOWN并持久halt，后续周期不能开始。不能靠在测试里提前排空成交队列隐去此竞态。

- 最小生产扩展仅`strategies/maker.py`：复用现有obsolete-cancel的精确终态证明，把它用于迟到的modify rejection；保持原身份、路由、native/store终态、逐笔成交已记账等全部判据。不按错误文案放行，不清旧HOLD，不接受工作中/未知/不一致订单，也不重新下单或补造终态。必要时只改私有helper名称/事件类型，不新增状态或框架。
- 编码agent独占上述生产文件及既有`test_maker_events.py`：先保留RED，再覆盖真实native处理的迟到拒绝及身份/数量/未决/旧HOLD负例；root保留普通双adapter连续反例及后续周期验证，reviewer不参与实现。MT5 helper两个文件保持冻结；其余生产范围不扩展。

**同根部分成交补充（第二反例确认后、再修改前）：** 真实MT5 poller启动后的24案为23过1失败，剩余为Maker第四周期partial1oz：PendingUpdate→Filled(partial)→PendingCancel→ModifyRejected（原生客户端因pending cancel拒绝旧改价）→Canceled；对冲已完成，源cancel也经原owner核对，但旧modify halt永久遗留。它与已关闭分支有区别，不能把pending cancel当终态。

- 仍只扩同一Maker私有proof：对modify rejection，若精确身份/路由/数量/全部已记账成交仍吻合、原业务记录为PARTIALLY_FILLED且仍是active源单、Maker已有fill冻结，且原生事件证明保护cancel仍未决，可保留原取消过程而不新增UNKNOWN。部分fill改变native status但不代表cancel应答，沿用现有原生cancel事件序列判据；已有拒绝/终态不得假作pending。这个分支不适用于cancel rejection、UNKNOWN或缺失/不一致事实，不放开工作源单或新准入。所有既有HOLD、inflight query和未完成对冲不变，必须继续等原cancel终态＋精确报告＋对冲完成才由旧逻辑释放。补实际native顺序的正/负例和原普通第四周期回归；不新增恢复状态、延迟队列或按reason字符串判断。
- 独立review追加真实反例：固定NT允许Canceled→PartiallyFilled的迟到成交转换；Accepted→Filled1→PendingCancel→Canceled→late Filled1无需篡改store，当前状态再次partial，但旧PendingCancel早已结束。短共享扫描必须被真实Canceled/Expired等已完成事实终止，不能仅看当前status或跳过历史终态来“复活”取消。原生真序列须使pending/protective proof为False，仍未决的两种partial排序保持True；同一有界写集修复，全量候选遇此REWORK即停止，不将其已跑部分记作全量通过。

**W5c1 Maker 单文件状态边界（2026-09-06，实施前固定）：** 先完成格式和原子写边界这一可独立审阅的小步，再实现残差分配与 bounded-carry；不把存储转换、净额算法和放宽准入混成一次提交。

- 一个 `<prefix>.maker.json` 文件持有 bid/ask 两个方向 view，复用 `JsonStateStore` 的订单、fill 去重、intent 和 hedge plan 语义。显式 Maker schema v2 在文件头绑定两个 instrument ID；旧单方向 schema v1 保持不变，不让 Taker 或旧 reader 默读新格式。此步保留两方向原残差数值和 strict 门槛，不声称 E04/E09 已完成。
- 每次实际 source fill 的持久动作同时保存该 fill/intent 与两个方向的已有或新增 freeze；不能在第一个 fill 文件写完、另一个方向尚未冻结时留下中间快照。两方向解除 freeze 也必须是一次原子写，仍须全部终态、对冲完成、零残差和原有唯一 freeze 原因证据。rename 前失败回到整份旧状态；rename 后父目录同步失败保留整份新状态并上抛，本次调用不继续发单、新源保持 HOLD；后续行情对已知义务的推进仍遵守原有对冲前提，不增设永久停 hedge 的新状态。
- 新路径不存在但任一旧 `.bid.json`/`.ask.json` 存在时明确拒绝启动；保留原文件，不自动清空或迁移。显式暂停/独占下的旧状态转换及同 route signed residual 的唯一分配记录在 W5c 后续实现；新文件存在时以其为准，绝不重新导入保留的旧文件。schema、品种绑定和双方向身份异常均拒绝，不做兜底空状态。
- 仅接通 Maker、离线 replay 汇总、live builder 与 canary 的实际路径识别和碰撞校验；canary 继续保留对旧文件的存在/输出碰撞检查。不改 EA、adapter、profile、账户、舍入算法、Taker 行为或 W6/W7。不执行真实 canary。
- 编码 agent 独占 `store.py`、新 `maker_store.py`、新 store 单测；root 维护本文、Maker 及入口/既有测试接线，reviewer 独立验证。先保留旧实现反例，再测双方向快照、fill/解除冻结的写前与 rename 后故障、重载去重/义务、旧文件拒绝及路径碰撞；最终全量/静态/独立审核通过后本地提交，不推送。

后续残差分配包的预审反例已固定：已有同 route BUY +0.5，再 SELL 0.6，应先合并为 -0.1 再按原舍入得0，不能先为 SELL0.6 分配 BUY1 后再抵消。BUY0.6 单独按原算法分配 SELL1、留下 -0.4，说明残差符号不等于方向 view。已分配 PENDING 也不准擦除；route 必须同时匹配文件绑定的两个品种及实际四个 account/client 字段，None 不作为通配符。W5c1 保留旧残差并 HOLD 的负对照不能计入净额验收。

**W5c2 同route原子残差分配（2026-09-06，实施前固定）：** 在W5c1的单文件上交付实际净额行为，先完成strict路径，再独立接bounded-carry预算和旧状态转换，不将尚未接通的能力计作完成。

- Maker schema v3增加有序的逐fill分配记录：完整fill key、实际signed成交量、不可变route快照、本次分配的signed源敞口量。每条与源累计/seen/双freeze/新intent同写；正分配对应SELL hedge，负分配对应BUY hedge。route残差从这份有序记录唯一导出，满足`R_after = R_before + signed_fill - allocated`，分配仍调用原`round_hedge_ounces`，不修改舍入算法；不复制订单或多腿对冲算法。
- route由文件绑定的两品种及实际source/hedge account+client限定，client None只与None相等，不作通配。缺account或绑定特定平仓票据的订单按原source CID隔离残差，不让未知账户或one-shot票据承诺跨单抵消。已分配PENDING/SUBMITTING等义务均不进入残差池；相反的未完成intent即使净和零也不解除HOLD。
- 两direction view仍持各自订单/intent，残差读数仅投影到该route最近一次真实fill的方向，另一view不重复返回该值；投影方向不是物理仓位归属。strict准入/解除冻结逐route要求残差为零，不能用不同route的总和零冒充完成。BUY0.6的负残差、±0.5边界、先+0.5再-0.6、反向镜像、跨route与已分配义务均覆盖。
- 不伪造旧历史：独立探针通过原v2接口证明相同CID/两fill key的(0.1,0.4)与(0.2,0.3)产生完全相同旧快照；旧累计和seen不能一般性恢复逐fill量，跨方向处理顺序也没有保存。v3明确拒绝现存v2或旧双v1，不自动转换、覆盖或空启动。暂停/独占下的显式迁移在下一独立包选择真实逐fill补证或明确legacy汇总checkpoint；不重跑新算法改写旧intent，也不洗掉UNKNOWN。
- 编码agent独占`store.py`的最小分配hook及残差读取接线、`maker_store.py`及其单测；root维护本文、Maker逐route全局gate和原生事件/普通策略净额集成测试，reviewer只读独立验证。先保留原同route抵消/真实fill历史丢失反例，再验证全过程原子故障、重载去重、原双终态后下一正常周期；冻结后全量/静态/独立审核，本地提交，不推送、改profile/EA、部署或实际发单。E04/E05仅按实际证据接受，E06旧迁移及E09 bounded-carry仍未关闭。

**W5c3 显式旧状态迁移（2026-09-06，实施前固定）：** 先完成历史起点与升级工具，再独立接W5c4有界carry。转换本身只变格式，不启动策略、不核销业务义务、不改运行profile。

- 新增仅离线调用的迁移入口：输入旧prefix，接受有完整品种绑定的单文件v2，或完整bid/ask两份v1；缺任一旧方向文件明确拒绝，不默认为空。v1没有品种证据，调用者显式提供两品种仅是操作人声明；v2必须与原头严格相等。输出必须为不同的新prefix，已有新/旧状态路径均拒绝；不覆盖、删除或回写原文件，不接受v3/v4再次转换。
- 有旧历史的输出使用明确schema v4及`legacy_checkpoint`；无checkpoint的原v3读写不变，旧v3 reader不能忽略新起点。direction表仍是唯一订单/intent账。checkpoint按旧CID只固定route、累计成交、旧fill keys、旧intent IDs和signed已分配总量，另保留输入来源SHA；不复制完整订单或意图，不捏造缺失的单笔数量/处理顺序。之后的`allocations`只记录新真实fill。历史余额的view投影采用固定bid/ask顺序的展示规则，不能称作最后历史fill；出现新fill后恢复原实际最近方向投影。
- 逐direction先验`sum(signed累计) - sum(signed完整已分配qty) == 旧raw residual`，包括COMPLETED、UNKNOWN及PENDING全部旧intent，绝不能减实际hedge_filled。再按不可变route/CID隔离汇总起点，所以不同route可以保留不同余额甚至大于0.5；不重跑舍入算法、不自动生成补偿hedge，strict仍按route HOLD。引用旧intent的side/总分配必须与checkpoint固定值一致，不能后来改数量再重算历史；其status、plan、已成交量、绑定ID、原UNKNOWN/HOLD/active/seen均保留。
- 旧累计和已知单笔也须相容：每CID有intent的已知source_fill量合计不得超过累计；无遗漏fill key时应相等，有无intent的key时差额必须正，filled与seen的有无一致。不反推各未知key数量。旧keys与新allocation keys不相交且并集恰为seen；checkpoint累计加新真实量等于源累计，旧intent引用加新非零allocation恰好覆盖全部intent。复用W5c2原身份/路由校验，坏输入留原件并报错。
- 代码检查证实普通Maker builder没有现成的全程进程锁，现有锁仅约束canary账户入口。这里不扩建锁平台：CLI要求操作人明确声明旧程序已停，读取前后复核输入字节稳定；声明/两次一致不冒称已证明无在场writer。文件发布用已flush/fsync的同目录临时文件→原子no-clobber硬链接→原父目录同步，不用exists后replace伪造不覆盖。已有目标或发布前失败不影响旧件，发布后父同步失败保留完整新件并报告耐久性未确认，不自动重试/覆盖。
- 写集：编码agent负责`maker_store.py`的checkpoint接线、新`maker_migration.py`与迁移单测；root维护本文、README、CLI注册、原`durability.py`/`store.py`窄发布原语及单测、普通Maker集成；reviewer只读独立复核。先保留v2/双v1不能继续的RED，覆盖未知历史同累计、两route历史rounding误分配、未决/UNKNOWN/plan保留、旧fill去重与新迟到fill、迁移后普通经济周期、输入变化/既有目标/发布前后故障。全量/静态和独立审核通过后单独本地提交；不推送、部署、修改EA/profile或访问真实账户。
- 复核补充：link和父同步已经成功后，临时名清理不再属于业务发布失败。清理异常只告警并保留临时名，CLI仍报告完整新件创建成功；不误报迁移拒绝或耐久性未确认。发布/父同步异常仍原样上抛，不能被这条窄清理规则吞掉。

**W5c4a 按实际分配顺序执行Maker对冲（2026-09-06，实施前固定）：** bounded-carry准入之前先修已确认的跨方向调度重排；本小步保持strict，不接新profile额度。

- 真实反例：同route两源各2oz，先后实际fill为BUY0.6、SELL0.2、BUY0.2、SELL0.2、BUY0.2、SELL0.2、BUY0.2。原分配为SELL1/BUY1交错七笔，但首hedge在途时其余入账，当前调度按bid全部→ask全部，实际会先SELL四笔再BUY三笔，中途MT5=-4、unhedged=-3.4。原selector与store API已复现，不以初始源leaves端点作为这种重排的预算证明。
- 新义务必须沿现成`allocations`实际fill分配顺序全局选择；零分配跳过，已完成跳过，每个多腿intent完整完成后才走下一个，已有in-flight/UNKNOWN/REJECTED/BLOCKED互斥与暂停保持。不得再建或持久化第二个队列，不以intent ID字典序、bid/ask或历史时间戳猜顺序。v4旧checkpoint无跨方向历史顺序：其未决义务仍整体HOLD待W6恢复，不能顺手重放；已完成旧intent保留且不阻塞后续新allocation。
- 编码agent独占`maker_store.py`、`strategies/maker.py`及各自单测；root维护本文与普通双adapter交错集成，reviewer只读审核。先将原selector真实反例转为RED，覆盖调度顺序、重载、旧checkpoint未决阻断、多腿完成与既有互斥；普通native/adapter必须实际展示交错对冲/票据结果，不靠手动改intent状态充当集成证明。
- 全量、静态及独立复核后本地单独提交，不推送、部署、改EA/profile或访问账户。只修执行顺序，不据此关闭E09；后续carry预算须在本顺序前提下同时覆盖分片舍入和撤单前双边late fill，不能只放宽残差gate。

**W5c4b 有界carry接线（2026-09-06，已完成离线验收）：** 在有序执行前提下连接余额资格、实际工作单预算和MT5预检；不靠禁用双侧Maker或自动消除dust简化验收。

- Maker配置新增`residual_mode`（默认`strict`）、`residual_limit_ounces`（默认0）、`max_unhedged_ounces`（默认None）。`bounded-carry`须显式给有限正预算和`0 < residual_limit <= 0.5`，先限定一个明确source/hedge账户route；其余非零route或CID隔离余额不得借用本额度。已有`CarryConfig`表示资金费率/swap，不混用。余额仍从原ledger唯一导出；strict默认和Taker不变，未决/UNKNOWN/原HOLD不因额度足够而放行。
- 上一cycle只有双源终态证据齐、原义务全部完成且未对冲量恰等于允许的残差时，才能一次性解除原双freeze。可从原store提取小余额资格hook；不得删除原已分配未完成量检查。这个终态要求属于上轮release，不额外要求新一轮第二侧入场时第一侧也终态；第二侧继续遵守W5b4的exact ACCEPTED、账户action与无partial/pending资格。
- 新单以native规范化数量计算；维护旧单按精确CID排除自身后放回候选，不重复计量。当前route的真实BUY/SELL leaves分别记B/S，不把相反挂单提前抵消，第二侧须重读第一侧已经占用的事实。新单和维护共用预算校验，不以报价意图代替cache/store事实，不重复从现金和方向容量扣同一挂单。
- `max_unhedged`单侧上界为`max(abs(R), abs(R + signed_Q))`；双侧均可能成交时取保守`0.5 + max(B, S)`，不是初始净端点。已复现`R=-0.5, B=2, S=0.1`：SELL0.1→BUY1完成后R=+0.4，再迟到BUY2会产生2.4oz未对冲，初始端点1.5不足。W5c4a有序完成前缀k满足`U=r_k+后续BUY成交-后续SELL成交`且`abs(r_k)<=0.5`，多腿部分执行在相邻前缀间单调变化；这是保守准入界，不是舍入策略变更。
- MT5容量与净仓风险检查采用有序净delta的保守端点：有BUY工作量时SELL上界`max(0,floor(R+B+0.5))`，有SELL工作量时BUY上界`max(0,floor(-R+S+0.5))`，经原动态容量、持仓上限/minimum/only_long规则验证。某侧工作量为0则相应对冲界也为0，不因R恰为±0.5虚造反方向义务；所有0量均跳过planner/quantity-ready。它们不是gross成交量或单腿手数上界：R=+0.5的BUY2整笔对冲2，分成0.2+1.8可对冲1再2、合计3，不能只按`round(R+Q)`校验累计风险。
- 单intent/lot预检与上述净delta分开：存在反侧working时，对每侧用`round_hedge_ounces(0.5+Q)`上界；没有反侧才按`max(0,round_hedge_ounces(sign*R+Q))`收窄。沿原`plan_hedge_delta`逐腿验量，不把累计3误当必须单腿3；R=-0.5的BUY1初算0，也不能在SELL1先成交后仍忽略它实际可能产生SELL2。现有已成交义务继续原对冲流程，不能用新准入预算切断它；实际下一腿仍重新读票据，snapshot到EA外部竞态不据此消除。
- 独立预审在实施期间补充票据演化反例：R=+0.5、BUY4、MT5初始BUY2，初始最大intent4规划为close2+open2虽符合单腿2，但真实0.2+3.8分片会产生SELL1再SELL4，后者close1+open3超限。另初始BUY2(id1)+BUY5(id2)、R=0、源B4/S0.2，初始SELL4的close2+close2通过，但BUY1.6→SELL0.2→BUY2.4分配SELL2/BUY1/SELL3后，会暴露id2上的close3。故保留当前planner逐腿检查，另验证每张初始反向票的`min(单intent上界,票据qty)`，覆盖小票耗尽后的大票；未来新open上界分别为SELL `min(K_sell,max(0,C_sell-P))`、BUY `min(K_buy,max(0,C_buy+P))`（P为初始真实净仓，C为上述累计净delta，K为单intent上界），仍经原quantity-ready校验。新open时反侧票已耗尽，单open不超过该端点总仓；后续新票close亦被这条量界覆盖。此法刻意保守，不引入成交路径求解器，不简单强制K小于单腿上限而禁掉纯减仓多票；须保留初始BUY4由两张2oz组成、R=0/BUY4纯减仓可通过的正对照。
- quantity-ready包含最小量和步长，不仅是最大量。有非零intent上界时同时校验原舍入单位1oz可执行；否则当前2oz上界虽通过，真实0.6oz部分成交产生的1oz对冲可能不满足新min/step。0义务不查1oz、不虚造交易。本包不修改strict旧行为，也不扩成任意broker手数规格求解器。
- 先离线验收strict不变、±0.5/更小限额/越界、异route、重载、双侧占用、分片与交错、lot和动态容量下降、正常经济机会跨cycle、带dust停止的真实数量报告。停止保留ledger和原保护撤单，不声称FLAT、不自动补偿交易。当前profiles/canary仍strict且不提高0.02lots；W9明定在线预算，W7另接Maker/Taker共享额度，不让两个实例各领一份残差额度。
- 实施分工：编码agent负责`config.py`、`store.py`最小余额资格hook、`maker_store.py`、`strategies/maker.py`和必要的短纯预算helper及对应单测；复用原经济容量判据，不另造账户或执行层。root负责本文、README和普通双adapter跨cycle验证，既有continuity夹具只增加构建前正常配置参数；reviewer独立只读验证。先记录strict残差阻挡及预算反例，再按上述契约实现；冻结后全量、静态与独立复核，通过后单独本地提交，不推送或操作账户。

**W5c4b 普通双adapter首轮9过1失败的窄修复（确认反例后、修改前）：** BUY0.5真实fill触发双freeze/双cancel；对侧SELL尚未成交，native实际为Accepted→PendingUpdate→PendingCancel→OrderModifyRejected→Canceled。终态owner核对成功、无hedge义务、源均CANCELED，但W5b6旧proof只接受本单partial，导致零成交对侧残留`maker modify rejected` halt，无法进入carry下一轮。

- 仍在`strategies/maker.py`原`_source_action_is_obsolete`内增加一个分支：仅modify rejection、业务记录ACCEPTED且filled=0、native确为PENDING_CANCEL，并保留active CID、原source hold/freeze、真实未结束cancel扫描及全部原身份/route/数量/已记账fill一致性。它只维持已在途的保护性取消，不将pending当终态、不清旧HOLD、不重发、不给新源准入；仍须原cancel终态与权威报告才能release。不按拒绝文案放行，不适用于cancel rejection或UNKNOWN。
- 编码agent保留原生确定性RED和负对照：无pending cancel、取消历史已结束、native/store成交不一致、route/身份不符、真正cancel rejection和已有halt；root保留上述普通双adapter自然事件顺序，不额外pump排空竞态或修改8秒截止时间。修复不扩大到adapter/wire/recovery平台。

**W5d 原策略经济对照（2026-09-06，已完成规定的离线验收）：** 收齐剩余纯经济规则证据，不重构执行层，不扩展多账户 live 支持。只编译已逐段阅读、哈希认证的原方法，注入合成账户/行情/时钟；不导入原模块、不运行初始化/联网方法、不复制含凭据的载体。CI 只读取入库的脱敏数值向量，不依赖本机 ZIP。

- Caller 对照覆盖严格阈值与 short-first（含 short 风险拒绝后不回退 LONG）、方向价格/数量/carry、Maker signed amount/spread/leverage、固定排序与候选跳过。严格分支用可精确表示的数值及相邻边界，数值容差不能用于改变触发方向。动态容量沿用 W5a 已认证向量；只对有效、已规范化、显式配置且 `keep_last_accounts=false` 的入口作等价结论。
- 已实跑确认 Maker 等仓位 tie 偏差：原 source 升序/hedge 降序后，bid 从前取、ask 从后取；当前 ask 反转排序但仍从前取，稳定排序使 tie 错选首个。先保留脱敏反例 RED，再仅修这两个选择器；不引入账户轮换管理。Maker 最终乘法价格、两 tick clamp、杠杆最低1及显式 FX 仍按既有迁移契约，不冒充原 callee 全链等价。FX 对照仅把当前 USD 参考价按 side 换算为原 caller 的可比价格。
- MT5 POINTS 对照明确映射旧 enum POINTS=0 → 当前 native POINTS=1、完整星期字典 → Sunday-first 七项倍率；覆盖 Athens 夏/冬跨日、DST、正常/三倍/零倍率、正负 swap，以及两策略实际决策时刻的引用。缺字段/未知 native mode 继续拒绝，不复制原缺失返回0、缺当天倍率默认1或缺整份倍率默认周三3的 fallback。
- 原 ZIP 不含 EA，旧 raw `swapMode` 是否曾重编号无法认证；同一 raw 0/1 在原 Python 与当前 native 含义不同，单列而不回退当前原生协议。原 Bitfinex getter 读取 `position.margin_funding`，当前已固定 `NEXT_FUNDING_ACCRUED`；仅认证同一显式 rate 的 `(f,-f)` 方向及 caller 使用，不声称 producer 相同，也不把预期 carry 当实收现金流。
- 写集限既有 Maker economics、紧凑脱敏 fixtures/对应经济与事件测试、本文及 README 历史状态澄清。主 agent 维护边界和核验，具体测试/窄修复委派实施 agent，另一个未实施 agent 独立复核。完整候选验证后单独本地提交；C07/E08 只关闭上述已认证规则与明确差异，W6–W9、A07 和线上验收保持未完成。

### W6：重启与停止形成真实闭环

范围：`store.py`、两个 execution 的报告/重连路径、live lifecycle、必要的原生 cache 配置和恢复测试。先执行 Q3。

**Q3 实施切口（2026-09-06，实验前固定）：** 本小步只增加紧凑离线测试与本文结论，不改生产恢复规则、profile、schema或默认准入。复用安装的1.231.0和现有adapter测试夹具，不接真实账户、不启动持久服务。

- 对照空cache只有原生reports、具备原`OrderInitialized`身份种子、完整原生事件三种前提。用两个StrategyId、同品种不同CID，逐项断言订单/position归属、MT5真实identifier、partial数量及TradeId，不以`reconcile_execution_state=True`单独证明成功；同品种双external claim明确测试是否支持。
- 复用真实MT5 execution adapter的合成EA journal/snapshot，验证报告保留票据与CID不等于保留原StrategyId；Bitfinex普通cold非零仓仍应保留既有拒绝，不打开close canary专用冷仓豁免来得到绿色。没有完整trades的partial cancel或已closed但数量不同的报告，只记录原生接口真实限制，不放宽adapter既有完整性检查。
- 将合成原生订单事件经原serializer交给全新Python进程，再由原生ExecutionEngine重放，核对两个owner、source部分成交、MT5票据及重复fill；同时检验仅`Order.apply`后`cache.add_order`是否重建Position。该实验只证明codec/原生重放前提，不冒充生产持久后端、掉电耐久、普通策略解除HOLD或R01–R06验收。
- 写集仅`tests/test_restart_ownership.py`、独立进程测试helper及本文；具体native reports/MT5测试可委派，主agent负责新进程实验和方案，未实施agent独立复核。结果写回Q3/W6再选择最小原生持久化接线；不因Kernel只支持Redis配置就自动部署Redis，也不临时造第二套订单日志。通过后单独本地提交这个技术前置检查点，W6父项保持未完成。

**Q3 观测结论与后续选择（2026-09-06，NT 1.231.0）：** 下表由`tests/test_restart_ownership.py`及`tests/restart_replay_worker.py`固定，14项离线测试已通过；完整集成和独立复核结果记入第10节。

| 恢复输入/路径 | 实测结果 | W6设计约束 |
| --- | --- | --- |
| 真实MT5 adapter、合成EA journal/snapshot，空cache只收reports | CID、两个position identifier及TradeId均保留，但两个原owner均变成`EXTERNAL` | 不从CID猜StrategyId，不把布尔成功/总量吻合作为归属认证 |
| 同上，先用原`OrderInitialized`及明确client/position索引seed | 保留两个StrategyId、两个identifier及完整成交量；重复对账不增加事件/成交 | 原生订单身份与索引须先恢复；不能对无证据的历史现场补造seed |
| 同品种的两个external claims | 第二个被`InvalidConfiguration`拒绝 | 不用双claim代替Maker/Taker订单归属；普通cold豁免保持关闭 |
| 原生CANCELED/EXPIRED报告有完整partial trades / 缺trades | 前者恢复1.25；后者可返回True但实际filled仍0 | 继续要求完整真实成交集合；EXPIRED只是native边界探针，不扩充BFX wire |
| 已CANCELED的原生订单收到更大累计量及新TradeId | 可返回True但仍只记原1oz | 比较实际cache事件/数量；落后闭单不能直接套用同run terminal确认 |
| 全新进程、原codec完整事件经ExecutionEngine重放，并恢复client/position索引 | 原事件UUID/TradeId、两owner、source部分成交、两个MT5 identifier、未成交exact-close绑定及TE来源标记保留；重复fill不改变状态 | 原生事件能承载所需语义；这不是持久后端、费用producer或普通重启验收 |
| 全新进程仅`Order.apply`后add_order / 重放时省略两个索引 | 前者订单完整但positions为空；后者已成交票据能重建，但未成交exact-close目标与client路由丢失 | 必须恢复原生Position和显式索引，不只保存OrderInitialized或订单JSON |

独立进程实验使用合成原生limit订单、BacktestEngine内核和NETTING Bitfinex/HEDGING MT5，不连接transport、不使用真实MT5下单路径。新进程预置同一合成Instrument/AccountState，只证明给定事件顺序的订单/仓位恢复，不证明账户/行情恢复、任意交错或netting翻仓。两个source策略仓位为+0.5/-1oz，是NT按策略的视图，不是Bitfinex账户出现两个venue仓位；共享净仓及交叉策略平仓语义仍由Q4/W7验证。跨进程临时文件只服务测试，不是生产恢复入口；普通Bitfinex adapter非零cold仓仍由真实adapter反例证明拒绝。

据此，后续W6按以下三个小步实施，不把Q3继续扩成研究阶段：

1. **W6a 原生cache接线与真实后端认证。** 优先使用框架现成持久化；安装源码`system/kernel.py:307–329`的配置入口只支持Redis，`cache/database.pyx`已有订单事件、Position和两个索引的读写，`ExecutionEngine.load_cache`会分别恢复它们。因此不新写JSON订单日志/Position重建器，不搬用测试worker做生产。后续先给现有builder接显式可选的原生cache配置；固定TraderId/namespace、`use_instance_id=False`、`flush_on_start=False`，与既有账户绑定核对，不能重启换namespace后假装空仓。真实后端连接、写入队列收束和跨进程load必须另行验证；本包没有安装/启动Redis或承诺掉电耐久，启用服务也不由测试通过自动授权。未接持久后端的原路径不冒称支持有仓续跑。
2. **W6b 已知事实恢复与原store幂等投影。** 在普通新源准入打开前，先核对恢复的cache身份/索引、venue完整历史/持仓、CID/EA journal及既有业务义务。MT5映射冲突必须在事件交给Engine前识别，因为native会优先用cache索引覆盖report中的position ID。缺失/冲突保持原HOLD；已知成交的重放仍经原生事件，原业务store只补实际缺少的义务。未决旧request不重发，reconciliation布尔成功不能单独解除HOLD；最终fee来源沿现有CID v2核对。真实后端未认证前可先实现/验证这层纯核对，不模拟出生产可恢复结论。
3. **W6c 在线drain与R01–R06。** 保持下列分类，按普通builder/策略在持久写入、submit、partial fill、hedge及stop边界重启，证明下一真实机会与不重复对冲；执行通道尚在线时drain，随后才最终stop。Q3的空引擎重放、stub报告、干净退出或现有canary均不能替代这些验收。

恢复分类：

- 已结束订单、对冲义务均完成，venue 仓位和本地记录吻合：恢复原策略运行，保留现有仓位。
- source 已有明确最终事实但 cache/业务 store 落后：通过原生对账补事件并幂等投影，不手改 filled_qty。
- hedge 明确未完成但已有真实成交：先核对票据与历史，再只处理剩余已确认义务；当前未决 request 绝不重新提交。
- source已成交而hedge明确REJECTED且成交0：先核对旧单确实无成交、执行前提已恢复，在同一原义务下显式创建唯一新hedge order/request ID；保留旧拒绝记录，不重放旧请求，不以清除REJECTED替代真实恢复。该恢复动作有次数/时间预算，不形成自动拒单重试循环。
- 仅“查不到订单”、历史不完整、身份不一致：仍 UNKNOWN；给出只读诊断和精确恢复条件，不删状态、不自动反向交易。
- 正常 stop：先停新源准入，撤本节点未完成源单并核对，处理可确认的已有 hedge；到预算仍未决则持久保留并返回未完成状态，不将超时当平仓成功。

drain 必须发生在 node/执行客户端仍运行时，完成或到预算后才调用最终 stop/dispose；不能在框架已断开执行通道后才尝试完成 hedge。信号退出与显式停止使用同一生命周期，Q6覆盖这个消息顺序。

验收：R01–R06，覆盖持久化/发单/成交/对冲边界的进程重启。没有原生 cache/order 归属探针结果前，不照搬 close canary 的 `generate_missing_orders=True`。

### W7：单节点 Maker/Taker 共账户

范围：live composition、`hedge.py`、策略 admission/状态 view 和集成测试。先执行 Q4；这是本轮完整目标的后段能力，没做完时只能交付“单策略可运行”。

目标设计：一个 TradingNode，每 venue 一个 data/exec client，一个 CID writer；两个策略保留自己的 StrategyId、经济逻辑和订单归属。以真实 account+instrument+route 聚合敞口，不以两个各自的净仓限额相加。

使用一个节点内 route 执行 lane：获得 lane 后才从最新 native positions 生成/绑定实际 MT5 腿；订单确认期间 lane 不交给另一个策略。预期外仓位变化暂停新源订单并启动有界核对，不静默继续，也不自动替账户清仓。

共跑时将该route的Maker/Taker残差纳入同一个既有业务状态原子单元，方向/策略保持独立view；不另造平行残差账。若已绑定完整多腿计划，lane持有至该计划完成或明确暂停，不能在两腿之间让另一策略修改计划所依赖的票据。调度按已发生的fill义务公平推进，避免其中一个策略永久饥饿。

依赖原生 cache 中的订单/仓位和现有义务投影，尽量由原协调器注入同一个 lane，不增加服务或第二个订单管理器。若 Q4 证明 native NETTING 与 StrategyId 归属不能直接协调，先记录反例并在本文修订最小设计；禁止把两个独立 runner 同时启动当实现完成。

验收：J01–J05。两策略必须各能真实参与，而不是长期阻塞其中一个；两边 Maker 工作单与 Taker 在途订单的最坏成交也不能超共享上限。

### W8：入口、发布和运行边界

范围：live entry、builder、公用运行函数、canary/smoke、`pyproject.toml`、README/协议、必要的脱敏示例与测试。

实施步骤：

1. 提供薄正式 Maker 入口，共用 Taker 的凭据加载、模式验证、生命周期；保留现有 CLI 兼容。`maker|taker|both` 是目标运行模式，不在实现前公布为现成命令。
2. canary 只提供数据/阈值/预算和断言；移除作为“策略验收”的 near-touch/claimed 后跳过改单覆盖。需要保留的 adapter 专项探针明确标注其验证对象。
3. fresh wheel 和 sdist 在临时环境安装，从仓库外运行所有声明入口及离线模拟，不靠 PYTHONPATH。当前主 .venv 安装元数据落后，不能当发布产物通过。
4. 清理 README/协议中的过时当前状态，将历史标明日期；保留一份现有能力表、启动/停止/恢复说明。记录支持的两 venue、币种/费率、净仓和 tickets 上限、状态格式与已知限制。
5. EA 两终端同 namespace 互斥实测、journal 写入故障测试；按预期频率和至少一个完整测试会话规模测历史扫描耗时、snapshot 大小、REP 延迟。先测再决定增量索引，不无依据改成异步 EA。
6. 在 protocol 64KiB 内制定容量 envelope：按最大合法字段长度验证允许票数，预留帧开销；准入覆盖拟新增票，在完整snapshot仍可读取的范围内停止新增、允许减仓。数值依据测量填入profile，不把样本220当通用上限。若已经超包上限，close前snapshot也可能失败，此时有界HOLD并定位恢复，不能承诺自动减仓；必要时协同提升已测算的Python/EA/ZMQ固定上限，不优先新建分页协议。禁止截断。

验收：P01–P06。可先完成构建/文档部分；不等这些维护项全完才开始 W1 修错。

### W9：有限 DEMO 连续验收

前置：W1–W8 对相应运行模式验收通过、当前 EA 与 Python/profile 匹配、无未解释活动订单/义务。暂停的自动 canary 不能自行拿旧 v4 profile 越过这些条件。

先做不发单连接/对账，再单独 Taker、Maker，最后同节点 both；均使用普通策略。每一轮都事先记录最大时长、最多源订单数、每单/累计净仓上限、最大未对冲量与超时、停止方式，使用已有两测试账户，不申请每一步重复授权。

建议初始会话预算为每模式 30 分钟、最多 10 次源订单；这是待运行 profile 确认的测试预算，不是立即执行命令。不得为了达到次数忽略市场关闭或不断重跑失败会话。跨日能力另外运行一个覆盖真实 broker rollover 的有界会话，结束时间按实际时区/市场时段设置；不伪称 30 分钟已证明跨日。

保留当前单 MT5 指令上限 0.02 lots。已有 2oz 净仓上限与“2oz 同向连续开两次”的场景不兼容：后者先在离线 4oz 容量场景验证；需要在线验证时，必须在会话开始前明确提高累计净仓上限（例如 4oz），不能顺手调高，也不能拿两个先开后平的 2oz 测试冒充连续加仓。

平仓是场景结束时的显式收尾，不是每个机会的策略规则。测试阈值可以降低以触发交易，但正式经济计算、事件处理和仓位逻辑必须保留。

验收：实际成交/订单与两边 positions 一致，义务不丢不重、正常终态后能继续、手续费 final/pending 清晰、退出状态准确。利润正负不影响流程通过；费用或仓位核对失败则不通过。

## 6. 可执行验收矩阵

这些编号只是测试场景索引，不增加新的测试框架。优先放入已有相关测试文件；确需跨模块集成可新增少量 `test_strategy_continuity.py` / `test_live_runtime.py`。语义失败测试先于修复。

| 编号 | 输入/故障 | 必须观察的结果 | 层级 |
| --- | --- | --- | --- |
| A01 | BFX TU fee=-0.061668 USD、成交2oz | 原生 fill=2、hedge恰好一次，fee表示0.06，raw不丢 | 真实 Engine |
| A02 | fee 为±0.005、±0.015、零 | 显式半偶舍入，WS/REST/恢复一致 | 纯函数+reports |
| A03 | MT5 commission=-1.25，零价差开平 | 费用为+1.25，原生Position不产生虚假盈利；返佣镜像 | adapter+Position |
| A04 | TE部分成交→TU→重复TU→REST | 总成交/hedge不变，final fee唯一，对账不误冲突 | Engine+adapter |
| A05 | TU早于TE、重复TE、同trade事实冲突 | 合法乱序幂等，身份/数量冲突拒绝 | Engine |
| A06 | fee持久化失败/重启后REST重建/来源标记缺失 | deferred Engine时序下fill及应有hedge仍各一次，accounting故障不关hedge gate；来源不足不猜provisional | 真实Engine+故障注入 |
| A07 | USD费、USDT结算、显式FX | 原币/换算与已入native费用分开，不重复扣减 | 报告集成 |
| A08 | TU缺失或final来源不完整 | 不伪造零手续费结算成功；hedge不无限等TU | Engine+runner |
| B01 | 中间ticket/symbol字段查询失败 | 整snapshot失败，Python不生成错误FLAT | MQL实际helper+Python |
| B02 | 枚举中票据增减/重复identifier | 有界重采或失败；无半列表成功 | 原生故障注入 |
| B03 | 完整成功空列表、已存在cache仓位 | 只对精确消失PositionId报FLAT | reports |
| B04 | REJECT/MARKET_CLOSED/NO_MONEY/REQUOTE/PRICE_CHANGED/PRICE_OFF，无矛盾order/deal/volume | rejected；EA非永久blocked；已有义务不完成；每码独立验收 | MQL+adapter |
| B05 | PARTIAL/TIMEOUT/CONNECTION/PLACED缺最终事实 | UNKNOWN/待核对，零自动重发 | MQL+恢复 |
| B06 | DONE但deal/order/qty不匹配 | 不伪造成功，精确close防护保留 | 原生helper |
| B07 | seek失败/短写/flush错误 | reserve不成功不发单；发单后终态写失败保留未知 | MQL原生 |
| B08 | 正常对账后重复request、旧UNKNOWN重启 | 幂等；旧未知不自动洗成拒绝 | EA journal |
| C01 | 同Carry/FX，新timestamp | Maker挂单ID不变、cancel=0，新鲜度续期 | 真Strategy事件 |
| C02 | 真值变更/同timestamp冲突/未来值 | 前者按经济规则改单，后两者不放行新风险 | Strategy |
| C03 | 仅MT5变价，BFX book约100ms旧且有效 | 发现LONG；随后同值BFX事件不重复发单 | BacktestEngine |
| C04 | 同场景但BFX过期/不可actionable | 不发源单，不重写source timestamp | Engine+live binding |
| C05 | swap变化/同值刷新、原Instrument对象被替换 | 两策略新引用、feed正常、funding时间不变；同值cancel=0，真变更撤旧单且确认前不补新单 | DataEngine+Strategy |
| C06 | contract/step结构变化 | 准入关闭，不以旧单位发单 | data+exec |
| C07 | Athens跨日/三倍swap/缺数据 | carry方向/倍率与oracle一致；缺数据不伪造 | 纯函数+事件 |
| D01–D02 | 普通Taker零成交IOC取消/过期→第二机会 | 权威核对后第二单可发 | 普通runner |
| D03 | 部分成交→cancel→late fill | 对冲真实总量，完成后能继续，不双算 | Engine+runner |
| D04 | venue已满成交但缺最终order事件 | 有界原生对账补终态，正常runner可恢复 | adapter+runner |
| D04b | inferred fill完成→迟到TU/REST→重启 | 数量/hedge不增加，synthetic与真实trade ID不混；费用完整可归属才final | Engine+费用恢复 |
| D05–D06 | 终态查询超时/错身份/成交数冲突/重复响应 | gate不错误释放，任务有界并去重 | live生命周期 |
| D07 | node停止时有query、inflight hedge | 任务收束/取消有归属，未决状态持久保留 | 停止集成 |
| D08 | 已知source fill但公共行情断开 | 新source暂停，已知hedge按执行前提处理 | 故障序列 |
| E01 | LONG2→LONG2→SHORT2 | 仓位2→4→2，非每步flat | BacktestEngine |
| E02 | MT5反向多票1+2，delta4 | close1、close2、open1严格串行 | 真hedge路径 |
| E03 | 第二腿前票据变化/某腿UNKNOWN | 停剩余腿，不自动重新规划旧未知单 | 故障序列 |
| E04 | Maker bid+0.5/ask-0.5同route | 原子净额抵消、零重复hedge、可释放cycle | state+Strategy |
| E05 | 不同route残差/已有submitted hedge | 不跨route抵消、不擦除在途义务 | state |
| E06 | 净额提交前后崩溃、旧两文件迁移 | 要么全部旧值要么全部新值，不半扣 | 持久化 |
| E07 | equity/margin变化、Maker工作单、已有12oz超10oz上限 | 新增风险受限，但允许减至11oz；穿零新开另算 | oracle+Engine |
| E08 | 严格阈值、short-first、carry/FX向量 | 与已认证原规则一致，未认证项单列 | 脱敏oracle |
| E09 | bounded-carry ±0.5边界、跨cycle累计、超限、带dust停止 | 同route残差持久且计入预算；超限关闭新源；strict不放宽；退出不假flat | state+Strategy |
| R01–R03 | 已结算持仓重启；source/hedge事件落盘边界崩溃 | 持仓/订单归属正确、不重发、不重复义务 | 真实node/cache |
| R04 | 查不到订单但提交结果不明 | 保留UNKNOWN，不以absence证明未提交 | 恢复 |
| R04b | source成交2、hedge明确拒绝成交0、前提恢复 | 原义务保留；核对后一个新hedge ID完成，旧请求不重放 | 真实恢复 |
| R05–R06 | stop有未决；合法恢复遇其它HOLD | 未决不假成功，只释放本项HOLD | 生命周期 |
| J01–J02 | Maker/Taker同tick及交错fill | 共享额度不超，一个MT5 lane，双方可推进 | 单node联合 |
| J03 | Maker双边工作单+Taker在途 | 最坏可成交敞口被预留，不仅看当前净仓 | 联合 |
| J04 | 非本策略仓位变化 | 暂停新源风险并核对，不静默忽略或自动清仓 | 事件/报告注入 |
| J05 | 共跑后停止/重启 | venue净仓与各策略归属同时一致 | 联合恢复 |
| P01–P02 | wheel/sdist仓库外安装，全部CLI | 无PYTHONPATH依赖，离线模式零网络/凭据读取 | 干净安装 |
| P03–P04 | 两MT5终端抢同namespace；原生写入故障 | 单写者有效；错误有明确结果 | 原生终端 |
| P05 | 最大字段/票据数/历史会话规模 | 快照不超协议容量，耗时支持会话预算 | 容量测量 |
| P06 | 真实Maker入口与专项canary对照 | 正式策略不依赖override，测试职责清晰 | 入口集成 |

## 7. 状态格式、兼容与回滚

1. W1 fee metadata 若改变 CID 根格式，写入明确 schema v2；旧 v1 可读且缺费用表示 UNKNOWN，不是零。v2 包含布尔 `accounting_conflict`，装载时不丢掉已知事实冲突。升级保持 account/CID/last_cid 和绑定不变；老程序应拒绝新格式，不能静默丢 fee 字段。
2. W5c3 已交付离线 `py000-maker-migrate`：完整旧双v1或单v2转换到不同prefix的v4历史checkpoint，保留旧件，原子no-clobber发布后才由操作人选择新路径；无历史checkpoint的v3读写保持不变。旧程序须停止，`--stopped`及前后输入字节一致只是声明/稳定性检查，不是普通Maker不存在的全程运行锁。转换不猜造逐fill量或跨方向顺序，也不解决UNKNOWN；失败不回退成空状态，发布后父同步失败保留完整新件，只有发布成功后的临时名清理失败告警且仍算创建成功。
3. 对含未决义务的旧状态，迁移格式不等于解决 UNKNOWN；先保留原语义，完成 W6 权威核对才改变业务状态。
4. Python/EA wire 不变时可分别发布；字段或闭集错误码变化则文档、codec、EA、配置一起版本匹配。`InpDeclaredSourceSha256` 使用现有 manifest 工具计算，新 hash 不是跳过匹配的理由。
5. 回滚先停止新源订单、核对在途订单与仓位，再选择兼容代码/配置。不能恢复旧状态备份覆盖升级后真实成交，也不能让旧程序读取不理解的新状态。
6. 若已交易后新旧格式不兼容，保留当前状态，采用向前修复或明确转换；不以 Git 回退替代业务恢复。不删除 CID/journal 来“解锁”。

## 8. 技术前置：有明确输入与结论，不设无限研究阶段

| 探针 | 已知/需验证 | 最小输入与输出 | 影响 |
| --- | --- | --- | --- |
| Q1 费用后补 | 最小codec/真实Engine探针已复跑；普通入口无cache database，选择CID v2来源证据；恢复矩阵待W1b | 原生事件codec落临时文件/重放、TE→TU、inferred→retire→late TU已验证；deferred回调/写盘失败/实际重启随实现验证 | W1第二部分；字段已写回4.1，不阻塞已完成W1a |
| Q2 动态Instrument | 原生DataEngine/Actor探针及主审复跑通过，接口/去重缺口已写入4.3 | subscribe_instrument/on_instrument；cache先替换，重复/倒序也回调，策略必须保留last-good | W3动态swap |
| Q3 重启归属 | 14项native/真实adapter/独立进程实验已通过，支持矩阵和最小原生cache选择写入W6 | reports-only丢owner、双claim不支持；完整事件+显式索引可重建，订单对象独自不能恢复Position；真实后端和普通策略release仍未验收 | W6a–c；Q3不等于R01–R06 |
| Q4 共账户NETTING | venue净仓和strategy视图可能不同 | 同node Maker/Taker相反fill，检查cache各视图、report reconciliation、共享lane | W7 |
| Q5 动态margin映射 | 已认证原ZIP及两端callee，2,000组normalized向量通过；原BFX flat producer存在除零，迁移政策/venue字段接线待固定 | flat/反向持仓/接近margin目标三组输入；不以normalized flat冒充原脚本可空仓启动，只补现有只读查询所缺字段 | W5动态容量 |
| Q6 普通终态补偿 | W4a–c已验证规定触发下的普通composition；Maker未发cancel且order/trade全丢的主动发现、完整启停仍待探针 | 复用原生open-order/query能力先验证；不新增轮询服务、不把native布尔值或组件停止当完整恢复证明 | W4/W6 |

每项只实现一组最小离线实验，结果写回对应设计段后再扩展功能。失败保留反例并只修相关方案，不把失败升级成重建平台。不能没有探针结论就宣称全功能已规划成“若干行即可”。

## 9. 执行组织、验证与停点

- 主 agent 负责范围、依赖、设计一致性和跨模块验收；编码交给明确工作包的 agent；提交前由未编写该改动的人复核语义反例。
- W1 与 W2 可并行，但共享 `mt5_v1_execution.py` 时分别由一个 writer 串行合入；W3与W4共用策略文件同理。子任务不能为加速在同文件互相覆盖。
- 每个提交应包含一个可说明的行为改进及其失败→通过测试，不设人为行数墙。源码重排、状态格式改变和现场验证分别保持可审阅；不把 W1–W9 塞成一个巨大 PR。
- 开发可在本地增量推进。commit/push/PR/合并和部署按当前任务明确指令执行，本文不是立即外部写操作。测试账户既定范围的常规步骤不重复索要授权；账户/资产、累计额度或真实资金范围改变才重新明确。
- 当前自动 canary 保持暂停，直到 W9 条件满足并由实际执行任务恢复；不是文档写完就自动恢复。

每个相关行为改动先跑 focused tests；每个集成提交运行：

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

EA 改动额外执行现有 `tools/mt5_source_manifest.sh --lines`、MetaEditor 编译及对应原生探针。正式安装验证在临时环境准备所需构建工具；当前 uv/build/hatchling 可用性需实际检查，不将 README 命令当已安装事实。不得为绿色跳过新增反例或降低旧校验。

停点规则：代码可继续本地实现并合并已验证独立包；未决外部事实只阻塞相关 live 阶段。出现交易结果 UNKNOWN，停止增加风险，保留义务并只做有界核对；未证实根因不重复同一 canary。源码错误先离线复现修复，再用一个新运行验证，不靠反复重试碰绿。

## 10. 完成定义与当前进度

三种结论必须区分：

- **基础语义已修复**：W1/W2反例通过，不能声称策略已完整迁移。
- **单策略连续 DEMO 已验证**：相应 W3–W6/W8/W9通过，但 W7未通过时明确不开放共账户同时运行。
- **本范围完整迁移已验收**：W1–W9及承诺的所有场景有当前证据，Maker/Taker实际普通路径持续运行、共跑/恢复/费用都符合本文；扩展项仍明确不支持，不许用延期来冒充完成。

当前进度：

- [x] 当前源码/审计事实重新核对，实施范围和根因梳理。
- [x] 费用原生表示/后补限制的只读接口检查。
- [x] 本实施规划完成交叉审查，并纳入费用来源/舍入分层、暂不可用恢复、拒绝码、route残差预算、降风险容量与拒绝后恢复的修订。
- [ ] W1 成交与费用。
  - [x] W1a：Bitfinex 小数分费用、MT5 原生符号与半偶量化；真实 Engine/Position/义务验证及独立复核通过。
  - [ ] W1b：Paper 最终费用、持久来源与最终会计。
    - [x] 核心代码已实现并通过集成及独立复核：TE/TU/REST、CID v2、来源/补账、冲突持久化与普通新源门。
    - [ ] A07 显式 FX / 最终运行 PnL 报告及普通入口报告接线；费用摘要不等于 native PnL 已被后补。
- [ ] W2 EA 原生事实。
  - [x] W2a/F7：六码保守拒绝分类候选、静态契约和实际 MQL helper 测试入口；独立复核及 MetaEditor 编译通过。
  - [x] W2a 的原生 helper 脚本：73 项分类检查实际运行通过。
  - [x] W2b/F3：完整快照、Python 暂不可用/恢复；独立复核及 195 项原生 helper 检查通过。
  - [x] W2c/B07：journal seek/完整字节/flush 检查及 294 项原生故障/调用链验证，原格式和 UNKNOWN 保持。
  - [ ] W2 的真实 DEMO 返回行为、跨终端互斥及容量边界；上述 helper 通过不等于这些现场场景通过。
- [x] W3 行情/成本（规定的规范化输入/离线范围）。
  - [x] F4：同值资金费刷新保留工作单，实际输入到期仍撤单。
  - [x] F5：Taker 任一腿新行情共享评估、同批去重、更晚行情继续加仓；真实 Engine 与独立复核通过。
  - [x] 动态 swap/Instrument：原生订阅、完整验证后发布、两策略 last-good 更新及独立过期；同值不撤单、真实变化撤旧单；独立复核通过。
  - [x] C07/W5d：已认证 POINTS 规范化公式、方向与 Athens 跨日/七日倍率；真实两策略事件回调对照及独立反事实通过。旧 wire 枚举、缺数据 fallback、BFX producer 差异明确单列，不将本项扩称原始生产链等价。
- [ ] W4 普通终态闭环。
  - [x] W4a：adapter 精确终态确认、部分撤单的实际成交前置核验、已应用成交的迟到 WS 去重；真实 LiveEngine 及独立复核通过。
  - [x] W4b 核心：两普通 builder 的共享有界核对、业务确认反馈与失败后恢复、canary 复用；D01/D03/D05–D06/D08 的下述离线场景及 D07 组件级停止收束，真实 LiveClock 线程边界通过。
  - [x] W4c：当前Paper同run IOC、已发cancel Maker的静默终态与直接零成交拒绝；双客户端重试的最新事实复核、撤单新动作期限；D02按实际IOC CANCELED协议语义验收，独立复核通过。
  - [x] W4d：当前Paper同run工作中Maker漏消息主动发现、完整真实成交集合核验、健康观察暂缓报价；普通双客户端与独立复核通过。
  - [ ] W4 剩余矩阵：D04b跨重启费用/原生事件恢复及完整node启停/信号drain归W6；当前组合的组件级stop与同run恢复不替代这些验收。
- [x] W5 经济/仓位行为与残差（规定的单策略、规范化输入/离线范围）。
  - [x] W5a：原容量 normalized Decimal 纯函数及脱敏固定向量，整数容量边界修正；独立复核通过，普通策略接线由W5b4交付。
  - [x] W5b1：账户原始事实、完整性和独立观察时间已实现；串联伪flat修复、普通组合与独立复核通过，仅交付事实入口。
  - [x] W5b2：Bitfinex原生QueryAccount按需联合刷新、有界去重与生命周期收束；普通组合及独立复核通过。MT5沿用既有完整刷新，不新增周期器。
  - [x] W5b3：两端完整账户事实到既有动态容量视图的纯映射；普通账户事件、经济产出及独立复核通过。
  - [x] W5b4：普通两策略自动消费动态容量/杠杆，按需刷新、操作后预算、Maker旧单与双侧维护、账户时效和原未决义务门接线；普通组合及独立复核通过。
  - [x] W5b5：两策略原生连续加仓/反向/穿零矩阵，以及真实MT5 adapter提交前计划一致性、在途/拒绝/未知阻挡；独立复核与全量通过，仅接受该离线分层范围。
  - [x] W5b6：两真实execution adapter的普通连续多票矩阵、原终态/账户刷新闭环与腿间故障；Maker迟到改价拒绝及历史取消判据修复，独立复核与全量通过，仅接受规定的离线范围。
  - [x] W5c1：Maker单文件/双view、fill与双freeze同写、原子release及新旧路径识别；独立复核与全量通过，strict残差语义保持。
  - [x] W5c2：同route原子残差分配、逐fill唯一账与strict净额；独立复核及普通双adapter/全量通过。
  - [x] W5c3：完整旧双v1/单v2显式转换成v4历史checkpoint，保留原件与未决义务；独立复核及全量通过。
  - [x] W5c4a：跨方向对冲按真实allocation顺序执行，不重排放大中途敞口；独立复核及全量通过。
  - [x] W5c4b：单Maker明确route的bounded-carry预算、普通双侧跨cycle及带dust停止报告；独立复核及全量通过，仅接受规定的离线范围。
  - [x] W5d/E08：脱敏原 caller 与规范化 carry 对照、Maker tie 修复及已知差异归类；独立复核与全量通过。W5仅关闭上述规定范围，共账户预算与完整启停仍分别归W7/W6，不认证原始 wire/账户生产链或现场运行。
- [ ] W6 重启/停止。
  - [x] Q3 技术前置：原生/真实adapter与新进程重放14项、全量及独立复核通过；支持矩阵和W6a–c方案已固定，不计作R01–R06。
  - [ ] W6a 原生持久cache接线与真实后端跨进程恢复认证。
  - [ ] W6b 完整事实核对、既有义务幂等恢复及普通策略解除HOLD。
  - [ ] W6c 执行通道在线时drain、进程重启/停止矩阵R01–R06。
- [ ] W7 同节点共账户。
- [ ] W8 入口/安装/文档/原生运行边界。
- [ ] W9 有限 DEMO 连续验收。

首次实施从 W1 的原生费用反例和 W2 的完整快照/拒绝分类开始；不继续旧 v4 canary，不先重构整仓。

### 实施记录

2026-09-05：用户要求无人值守实施。已创建当前任务每30分钟的实施续跑 heartbeat（`arbi-nautilus`），旧 `resume-maker-v4-canary` 继续暂停。代码工作由独立文件范围的实施者推进，主 agent 集成，另一个分工只读复核。当前开始 W1a：Bitfinex 费用精度和 MT5 费用符号/量化，先验证失败反例；Paper 最终费用与其 schema 仍为后续 W1b，不混作已完成。没有提交、推送、部署或交易。

2026-09-05 / 第一批本地集成完成：

- W1a Bitfinex：WS/REST 复用 `usd_commission`，原始小数分不再拒绝成交；转换失败不消费数量/去重记录；真实 ExecutionEngine → HedgeCoordinator 只产生一次成交和义务。USD commission 与 USDT native PnL 明确分开，不宣称自动换汇。原失败反例组 15 failed → 19 passed；追加边界后相关三文件 179 passed。
- W1a MT5：实时/历史统一将 native cashflow 转为 commission；所有成交参数在 accepted / pending 移除前构造，成功回调前仍释放 pending。原失败组 12 failed → 13 passed；整文件 101 passed，包含同价开平的原生 Position PnL。
- F4：仅删除“新 timestamp 即经济变化”的判据，无新增 timer 机制。已有旧 timer 醒来按最新 cost timestamp 重新判断并重排；真实 TestClock 验证 cost/quote/session 三种先到期边界，行情和 session 不随成本刷新延寿。新增反例 4 failed → 4 passed，Maker events 整文件 58 passed。
- 独立复核：未写上述代码的 reviewer 已接受 W1a 和 F4；最终 BFX 补强回归再次复核通过。
- 主 agent 当前合并工作树全量验证：**965 passed / 12 warnings / 10.03s；Ruff 通过；Mypy 66 files 通过**。这是本批实现的结果，不是审计基线 935 项。12 个 warning 来自既有 Pandas/框架弃用提示，本批未隐藏。
- 本批 Python 生产净增 8 行（`git diff --numstat -- src`），无新平台/服务/状态格式；下一步继续 W2 的明确拒绝分类与完整快照，W1b 仍未关闭。当前仅本地未提交变更，没有部署新 EA 或发单。

2026-09-05 / 第二批本地集成完成（W2a 代码与编译，不是 W2 完整验收）：

- `Py000ExecutionResultIsRejected` 用六个已列明的 server retcode、零 order/deal、有限零 volume 和零 external retcode 判断明确拒绝。其它结果进入原有完整 DONE 证据检查或 UNKNOWN；reserve-before-send、历史 replay、exact-ticket 校验未改变。生产 EA 净增 27 行。
- 三个新静态契约从失败变为通过；协议/manifest focused 130 passed，独立 reviewer 的 16 个定向测试通过。新脚本 `mt5_ea/tests/Py000ExecutionRetcodeTest.mq5` 直接调用实际 MQL helper，设计覆盖 73 次分类检查，不调用交易、EA 初始化或 journal。**尚未运行该脚本，73 不是通过数。**
- 主 agent 在 Windows 11 独立临时目录、使用 MetaEditor `5.0.0.6162` 编译主 EA 和测试脚本：两者均 **0 errors / 0 warnings**，分别 7097 ms / 1083 ms。编译日志和二进制保留在本机 `runtime/mt5-native-w2a-Z4jBxI/`；临时脚本只复制源码/编译/收集产物，不替换正在挂载的 EA。
- 该批候选 source manifest：`92fb5420e80d938b01709a55594e582b74e2f965c0bea1ff2784f4c3b8f7edf5`。主 EX5 SHA-256：`a1c62be8489f9cb69caf1de20f2ccfe23b9cac4b34154e86951e7fe08c1f8be4`；测试 EX5 SHA-256：`ab417440859fa622d4dee4b20ff444d9bf991a0d78e95348eb0a976ed501c3d5`。这不是已部署声明；现有 profile 不自动换成新 hash。
- 本地当前合并树再次全量验证：**968 passed / 12 warnings / 9.93s；Ruff 通过；Mypy 66 files 通过；git diff --check 通过**。Python 与 EA 生产代码合计净增 35 行，另有针对性测试与本文，不增加服务或执行平台。
- 后续先并行推进不重叠的 W1b 最终费用、W2b 完整 snapshot；F5 可以随后单独修复。W4 不抢写尚在 W1b 修改的 Bitfinex execution。W2a 原生脚本运行尚缺证据，但不阻止其余离线整改。
- 再次核实实施 heartbeat `arbi-nautilus` 为 ACTIVE、每30分钟；旧 `resume-maker-v4-canary` 为 PAUSED。当前未提交、推送、部署或交易。

2026-09-05 / Q1 最小前置探针：实施者提供 `/tmp/py000-q1-fee-probe.E5C92v/probe.py`，主 agent 通读并独立运行 `PYTHONPATH=src .venv/bin/python /tmp/py000-q1-fee-probe.E5C92v/probe.py`，exit 0。该临时文件是探针，不是持久发布入口；后续回归应落实在现有测试文件。

- 实际 adapter/ExecutionEngine 产生的 TE `OrderFilled.info`，经安装版本的 MsgSpecSerializer（msgpack/json）、临时文件和 OrderUnpacker 事件重放后仍保留 `te_paper / pending`，数量 2oz、佣金 `0.00 USD`。TU 后原生事件仍 pending/zero，最终 raw fee `-0.061668` 仅进入易失 `_seen_trades`。
- 实际 LiveEngine inferred fill 的 ID 为 UUID、info 为空、commission 为 `0 USDT`、liquidity 为 `NO_LIQUIDITY_SIDE`。retire 后迟到 TU 当前被忽略，native fill 仍一次、CID binding 仍在；这证明存在待修路径，**不是费用恢复已通过**。
- 两个普通 builder 均未配置 cache database；安装 kernel 在此配置下不建立持久 cache。探针只认证当前 serializer/事件重放接口，不冒称 Redis、掉电或真实进程重启验收。
- W1b 接着覆盖：已应用原生事件的 scoped 回调与 deferred 顺序、fee I/O 失败不阻 hedge、v1→v2、TE/TU 边界重启、inferred→完整多 trade 集合。当前 REST 历史回溯上限14天，超窗 pending 不承诺自动重建。
- Q1 后的字段/恢复边界修订已由未写探针的 reviewer 独立复核；费用补证据与 W6 原生事件恢复的责任已明确区分。

2026-09-05 / W2b/F3 本地代码与原生 helper 验证完成：

- EA 两轮读取每个票据及目标 symbol 的必要字段，比较无序的 ticket/identifier/symbol/magic/side/原始 volume；浮动价与盈亏变化不误判。失败输出不保留半列表，只有完整且两次一致的空集合才返回 `[]`。`SNAPSHOT_UNAVAILABLE` 只允许出现在 `get_snapshot`；字段格式或身份异常仍按原严格失败处理。EA 本包生产净增 104 行。
- Data 保留 last-good 仅用于诊断、发布 NOT_AVAILABLE、不刷新健康时间；新订阅不能重发旧 TRADING 状态。Execution 暂停新源和所有权威报告，保留连接；只有完整 snapshot 和未变化的 journal tail 才恢复。启动未有完整快照时不宣称连接成功，后一次正常 connect 可恢复。平仓前 refresh 失败不消费 request ID、不发单；已有 UNKNOWN/foreign-magic HOLD 不随 snapshot 恢复被清空。
- 初始 Python 反例 9 failed → 相关三文件 144 passed；独立 reviewer 追加发现 mass-status 前置 journal timeout 仍可出旧报告，主 agent 复现 RED 并修复。补上前/后 snapshot 超时、仅 journal 成功不误清、UNKNOWN/foreign 保留后，三文件 **147 passed**，Ruff/Mypy 六文件通过；独立 8 项定向返工复核通过。这不是本轮全仓最终 gate。
- MetaEditor `5.0.0.6162` 在独立目录编译主 EA、拒绝分类脚本、快照脚本，均 **0 errors / 0 warnings**，耗时 6378 / 1218 / 1917 ms。当前 source manifest 为 `accef68041ec17a8912e73903ecac6f3b6a7aced7c13bb2e04472bf903e57666`；主 EX5 为 `5e77703ffd5e064306310adfb2d67cab4c2752344c522d0b358a8d07540f181f`，拒绝脚本 EX5 为 `b6c3544bd2a68169ec16595ee28ab6bca8ab3937af26a80ae0dce8c2738f82e5`，快照脚本 EX5 为 `3417892e92a122bc60e9f61c6dcfed90b9ad57e03473c957c261dd411250bf7e`。未替换部署文件或修改 profile identity。
- 两个实际 MQL 脚本在独立 portable 终端、Login=0、禁交易/禁 DLL 条件下运行，日志分别为 **`checks=73 failures=0`** 和 **`checks=195 failures=0`**，脚本及终端均 exit 0。只使用安装包公共 EURUSD 样例符号/历史，没有复制账户、配置或已部署 EA。原终端进程 PID 2696 在前后保持不变。启动采用官方支持的 [portable/config/StartUp Script](https://www.metatrader5.com/en/terminal/help/start_advanced/start)；这不是 broker 发单验证。
- 本机编译、源码复制及运行证据位于 `runtime/mt5-native-w2b-ro1K24/`。首次 SYSTEM session 0 不能创建 MDI 图表，两测试各 30s 截止后仅停止对应新进程；失败日志保留。查明会话问题后，用 Parallels `--current-user` 的 session 1 运行上述成功样本。两次均有非致命 MCP 端口冲突/内嵌编译器提示，未将它们隐藏成无诊断日志；通过依据是实际脚本计数及退出码。
- W1b 最终费用、F5 正在不同文件范围继续实施；尚未提交、推送、部署或交易，旧 canary 保持暂停。

2026-09-05 / W3/F5 本地集成完成：

- Taker 的 source L2 / MT5 quote 共用 `_evaluate_and_submit`；source freshness 使用原生 book 的 `ts_last`，不会被另一腿更新。MT5 事件仍先推进已有对冲，再评估新源。生产策略净增 12 行，没有新增经济公式或执行状态机。
- 真实 BacktestEngine 先复现仅 MT5 更新的 LONG/SHORT 均漏下单，修复后各有一次 source 与 hedge。另一反例在同步成交/对冲完成时同一时刻三次回调会开三笔 source，改为第4.3节的运行态同批标记后只开一次；后一真实 MT5 时刻仍继续加到 ±2oz。数量前提先拒绝、同一时刻再满足仍可下单；无效时间/陈旧或浅 source book/未决 hedge 均不误放行。
- 两个原有模拟载体的初始 source 报价本身在首个 MT5 tick 就存在机会，原测试依赖旧漏触发才等到 3s。将它们初始报价改为中性值，只维持各自“一次机会”与“开仓后精确票据反向”的测试意图；新 Engine 回归独立验证首个 MT5 tick 触发，不通过延迟触发掩盖问题。
- 主 agent 跑 events/cost-input/hedge-planning/backtest 四文件 **58 passed / 36 warnings / 0.95s**，Ruff/Mypy 五文件通过。独立 reviewer 18 项定向测试通过，包含 one-shot、实际新源 readiness、单次 now 捕获和精确票据反向。warning 增加来自这些新增 Engine 场景触发既有 Pandas 弃用提示，没有过滤。
- 本包未声称动态 swap、普通 IOC 终态恢复或跨进程恢复已完成；这些仍留在对应工作包。

2026-09-05 / W1b 费用核心本地集成（不等于 A07 或 W6 完成）：

- 费用事实复用 account-bound CID v2，区分 cache 已应用的 `native_fills` 与真实 `venue_trades`，通过既有原生事件发布回调逐笔核对；没有新数据库、服务或通用账本。TE 仍及时产生一次真实成交及对应义务，TU/REST 只补最终费用；重放不再次增加 native fill/hedge。迟到且缺 CID 的 TU 可凭已拥有的唯一 venue order 绑定归属。
- 保留 raw fee 与逐笔半偶量化、原生已记费用的分币种差别。无 native 可靠覆盖时 booked/correction 为 None；inferred 要求同订单每笔方向一致以及数量/价格加权覆盖。仅当前 cache 已确认的零成交 canceled/rejected 才从费用未知中排除，重启缺证据的旧 CID 不猜零。无 info 保留 unknown，只有已核实 Paper + zero USD 的特定来源才允许补 provisional 差额。
- 真实 deferred LiveEngine 回归证明：入队不当成已入账；fee 写盘失败时 reader、真实 fill 及持久 hedge 意图仍继续。记录 I/O 后续完整重放可恢复；raw-fee-only 冲突单独关闭 accounting 新源门，不把后续已知成交的 hedge 停掉。普通 Maker/Taker 已接该门，已有 close-existing 降风险例外保留。
- 独立预审发现并关闭了 inferred 净额掩盖混方向、缺 native 猜零、无 CID late TU、正常零成交终态、仅凭 provisional 标签放行等反例；该冻结候选独立 22 项窄测试通过。随后 reviewer 以真实 Engine/新 client/同 CID 文件复现“费用冲突后，重启并成功核对空近期历史又变 complete”，因此追加单向 `accounting_conflict`。标记先在内存保留再尝试原子写，写失败不阻已知对冲；新 client、旧值重放、成功 CID 分配不能洗掉已持久冲突。最后 30 行生产增量已获独立窄验收：13 项相关回归通过，原独立重启反例原样复跑保持 ready=False / summary=False / execution_hold=None，原 raw fee 不变。主 agent 接受本核心包，不代为关闭 A07 或 W6。
- 最终相关六个测试文件 **243 passed / 3s**，Ruff/Mypy 11 文件通过。主 agent 对冻结集成树全量运行：**1020 passed / 42 warnings / 10.60s；Ruff 全仓通过；Mypy 66 source files 通过；git diff --check 通过**。EA manifest 仍为 `accef68041ec17a8912e73903ecac6f3b6a7aced7c13bb2e04472bf903e57666`，上述两原生 helper 的 73/195 项结果继续对应当前 EA 源码。
- 明确保留未完成项：A07 显式 FX 的最终运行 PnL 报告及消费接线、原生持仓/订单的实际恢复 W6、动态 swap W3、普通 IOC 终态闭环 W4、journal 原生故障/容量 W2 与后续 DEMO W9。费用证据持久化不是这些能力的替代，不自动恢复 canary。
- 实施续跑 `arbi-nautilus` 已再次核实 ACTIVE、每30分钟；旧 `resume-maker-v4-canary` 仍 PAUSED。所有改动仍为 `codex/audit-remediation` 本地未提交候选；没有 push、部署、账户配置改动或交易。

2026-09-05 / W2c/B07 本地实现与原生验证完成：

- 先在独立 portable MT5 实测 5 组原生 UTF-8/CRLF 字节、FileWriteString 返回长度与 FileFlush 错误：36 checks / 0 failures；事实探针证据 `runtime/mt5-native-w2c-r42wRG/`。按该结果仅改现有 writer：检查 seek、实际 UTF-8 完整字节及 write/flush 错误，保留 TXT 格式、不重试、不改旧 UNKNOWN，生产净增33行。
- 最终脚本 include 实际 Journal/Execution，OrderSend/OrderCheck 在脚本中替换为永不交易的计数 stub。独立临时文件覆盖 seek false/error、短写/零写/write error/flush error、reservation 失败发送次数0、终态失败不确认完成及旧 UNKNOWN 重载/replay。测试只删除本次确认新建且断言通过的 `PY000_W2C_PROBE_*` 临时文件；失败留档，不接触实际 EA namespace。
- 主 agent 阅读实际实现/脚本，在 MetaEditor 5.0.0.6162 独立编译主 EA 与 retcode/snapshot/journal 三脚本，均 **0 errors / 0 warnings**，分别6616 / 1070 / 1867 / 3156ms。三个隔离原生运行分别 **73 / 195 / 294 checks，全部 failures=0**，脚本和 portable 终端 exit0；已有终端PID2696前后不变。另一 agent 直接解码全部日志复核一致。静态协议/manifest focused 136 passed 不替代这些原生结果。
- 当前 source manifest `1774a305c82bb29bee1290b38661dc22eb9193e7f61662074cadfb3522ac8ed2`，主 EX5 SHA-256 `4ed7ff53ef1271cbcad3bdaa88bc1309c940242919dad84118ddc75c1b07892f`，journal test EX5 `794dc430460f9e5503c3d07a8a5cf8dc9c23adc0af1828d4fd4c464100f46de1`。当前编译/运行证据 `runtime/mt5-native-w2c-final-AVwepG/`；旧 W2b hash/EX5 只代表此前样本，不能用于本次候选认证。
- 本包不声称 broker 故障、断电 fsync、容量/两终端互斥已验收。未更换部署文件或 profile，未提交、推送或发单；W3 动态成本与 W4a 终态精确核对在独立范围继续。

2026-09-05 / W3 动态成本与 last-good 本地集成完成：

- MT5 data 使用原生 Instrument 订阅/发布，将 swap 数值、mode、rollover vector 与 contract/tick/lot/身份结构分开。先验证整份 snapshot、时间、结构和交易状态，再提交 provider/健康状态及发布新对象；同值的新观测续期，倒序、未来、同时间冲突或无效结构不覆盖 last-good。执行侧继续允许动态 swap、拒绝真实单位变化，没有新增数据总线或修改 Nautilus。
- 两策略订阅并验证新的 hedge Instrument，自身保留 last-good 引用；Instrument 与 funding 时间独立。共用74行纯 helper 消除重复解析。Maker 同值刷新保持原工作单，真实成本变化撤旧单，最终确认前不补新单；无效成本只关闭新源，不清除已经发生的 hedge 义务。
- 独立复核实际复现两处遗漏并返工：snapshot 的 authority trade mode 冲突原先可在发布新 Instrument 后才报错，现已无条件前置校验（无状态订阅者也适用）；较新但已过期的 funding 原先会覆盖 last-good，现对公开更新与原生事件统一验证当前时间和期限。原反例及8项时间边界回归通过，合法后续观测可恢复，未来值不污染恢复时间水位。
- 实际 DataEngine/Strategy 事件测试、141项策略定向测试及数据/执行边界测试通过；未参与相应改动的 reviewer 独立复跑原反例并建议接受。仅有原 helper/oracle 回归通过，C07 的新增全量原策略对照仍留 W5，不据此关闭 W3 的全部经济 parity。

2026-09-05 / W4a adapter 终态精确核对本地集成完成：

- 在送交原生 Engine 之前核对 partial-canceled 的完整真实成交集合、方向、身份、数量及价格覆盖；缺 trades 时返回核对失败，不能由原生返回 true 或订单 canceled 推断成交为零。已核实终态再与 cache 精确比较，并只释放对应 adapter 等待；其它 UNKNOWN、费用冲突和业务 hedge gate 保留。
- 原生 inferred fill 已应用而 adapter 尚未显式确认的窗口，迟到 WS 不再重复调用 fill publication，真实 TU/REST 仍能补费用证据。没有修改原生 Engine、重复补发成交或扩大费用冲突豁免。
- 独立 reviewer 新增 Paper Maker 反例：已验证撤单流省略 post-only 位为0，但 REST 保留4096。仅对该已认证省略规范化派生报告，原始 WS/REST 不变；反向丢位、非 Paper、未认证省略以及身份/价格/均价/reduce-only 矛盾仍拒绝。原反例独立复跑通过，42项新回归通过；相关四文件260项通过。该包生产净增71行，没有另建订单状态机。
- 主 agent 原样复跑 Q6：full/partial 的真实补成交各一次、已应用窗口额外 WS publication 为0；partial 缺 trades 时 native reconciliation=False，cache 仍 ACCEPTED，业务/adapter gate 不误解锁。Q6 的显式模拟 hedge 完成不冒充 D01–D08。原生缺整体 timeout/single-flight 的观察仍成立，须继续 W4b。

2026-09-05 / 本批冻结集成树验收及下一停点：

- 首次全量回归为1104 passed / 3 failed；三个失败均为旧隔离测试载体未声明新增的 Instrument/readiness 字段，真实构造器已有正确初始化。仅补齐载体的固定初始观测/运行模式，保持原来的过期、未来、headroom 与拒绝断言；独立 reviewer 原样复跑三个节点通过，未放宽生产检查。Paper 返工随后另增加9项测试。
- 所有返工冻结后的实际全量结果：**1116 passed / 56 warnings / 12.18s；Ruff 全仓通过；Mypy 67 source files 通过**。warning 为现有 Pandas/框架弃用提示，没有过滤或跳过失败。当前 EA 原生73/195/294项及 manifest 对应上方 W2c 最终产物；未部署。
- 下一包明确为 **W4b 普通运行路径接线**：按4.4复用原生全量对账，以客户端所有的一个共享任务加外层期限，订单等待者去重；精确 adapter 确认后才逐项核对业务义务，验证第二次机会及停止/取消。先写真实普通 composition 的失败场景，再实施；不继续扩展已完成的局部 adapter 包。
- A07、C07/W5、W6、W7、W8现场边界和W9仍未验收。现有 `arbi-nautilus` 每30分钟实施续跑保持 ACTIVE，旧 canary 保持 PAUSED；本地分支仍为 `codex/audit-remediation`，未提交、推送、更改账户/profile、部署或交易。

2026-09-05 / W4b 普通运行核心、组合竞态与真实时钟边界完成：

- 两普通 builder 共用 `SourceTerminalReconciler`（Nautilus Actor 子类）；每个 node 一个实例、客户端所有的一个根任务。排队前同步暂停新源，原生两客户端对账、adapter 确认和业务确认都在每轮整体期限内；最多两轮，同失败订单/episode 不被重复事件重置预算。已有明确 hedge 不受该新源 gate 阻断；MT5 被取消后若已断连则不盲跑第二轮。本模块267行，没有新增服务、数据库、订单状态机或原生库改动。
- 两策略共用80行纯终态比较与 callback 类型。Taker 补齐取消/过期查询；Maker 删除旧私有无界查询，canary 复用相同 runtime。`store.update_source_status` 只在没有其他 halt 时加入相应 source 核对原因，防止终态覆盖并随后误清 restart/hedge HOLD；原样4个失败反例及6参数持久化回归通过。
- 独立复核发现并关闭两处跨组件遗漏：业务回调原先失败后吞异常、丢失 inflight，runtime 却返回成功；现仅明确 `True` 且确认落盘成功才删除该回调，失败保留原等待者，显式恢复仍可交付，已成功回调不重复执行。另一处 canary 将首轮失败的暂态 `last_failure` 当最终失败；现 `reconcile(retry_failed=False)` 加入在途根任务，已耗尽失败则不重启。真实 Maker/Taker 错报告、实际 `_persist` 失败、修复后恢复及真实 Actor 退避中的 canary 控制流均有反例回归；独立审核接受。
- 普通 Maker 组合抓到保护性 cancel 排队晚于 native `fill → canceled` 和 adapter retire 的实际竞态。原处理会把迟到 cancel-rejected 写成 UNKNOWN，真实 hedge 完成后仍不能继续；现只在4.4列明的 owned/native/业务事实一致时保留既有终态和所有 HOLD。不是按 reason 字符串放行，也不吞 adapter 拒绝。独立恢复旧 handler 得到11个定向 RED及两个普通 Maker RED；当前28个正负回归通过，身份/路由/数量不符和既有 UNKNOWN 仍保持 HOLD。
- 真实 LiveClock 的线程归属是本批实证补出的根因：旧 runtime 在 debug 模式跨线程创建任务报错，非 debug 模式要等650–700ms的别的唤醒；4个无轮询、500ms截止的真实 builder 测试先 RED。现 timer 只投递回所属 loop，asyncio/uvloop × debug开关4项通过，通常约252ms开始；stop前已排队事件不会再造任务。普通 Maker 的旧 stale timer 同样直接从 Rust 线程修改策略/store，另作27行窄修；8个真实时钟/停止/重启场景从 RED 到 GREEN。独立移除 generation 检查仍可使相同工作单ID的重启反例变红。原 deadline、freshness、撤单规则与 TestClock 同步语义保持，Taker 未新增 timer。
- `tests/test_strategy_continuity.py` 最终8项使用普通 Maker/Taker builder、真实策略/DataEngine/RiskEngine/LiveExecutionEngine 与实际 Bitfinex adapter。首单和下一单均由行情/经济逻辑创建；下一单必须 native `SUBMITTED`，且实际模拟 wire 的新增 `on` 恰好一次、CID与持久绑定一致、数量和 symbol 正确，不能只凭业务记录数量算成功。partial1/2的缺失 WS fill 由模拟 REST 事实经原生对账补齐；迟到 TU 双次重放不重复 hedge。MT5 注入的是 venue I/O，通过实际 client 的原生事件生成器产生 submitted/accepted/filled，成交价、native持仓均价和位置报告均为3936.7；不直接写完成义务。
- 上述8项还覆盖公共源行情断开时已知 hedge 继续、恢复前不发下一源单，以及 query 与 hedge 都在途时实际 Trader.stop/client drain 保留 JSON 未决1oz。stop测试使用空闲 loop 的 Event 等待，不靠5ms轮询唤醒 timer。独立复跑8项通过，并额外采样四个 Maker/Taker零/部分成交组合的真实第二次 wire/CID 映射。范围明确为 source LONG / Maker bid 单方向离线组合；成本/session/连接和 MT5 I/O 为显式夹具，不代表第二单 venue ACK/成交、MT5 wire/EA、完整 kernel启动对账、信号退出或 W6 重启验收。
- 所有返工及最后 wire 断言冻结后的主 agent 全量结果：**1224 passed / 80 warnings / 16.89s；Ruff 全仓通过；Mypy 71 source files 通过；git diff --check 通过**。80个 warning 为已存在的 Pandas/框架弃用路径随新增 Engine 场景触发，没有过滤。runtime、策略、canary、普通组合与两个真实时钟窄包分别由未参与对应改动的审核者复跑反例，主 agent 接受本批限定能力。
- 下一步先补 W4 剩余矩阵的普通过期/静默终态能力证据，逐项区分当前协议不产生的终态、可由既有权威历史恢复的情况和必须保留 UNKNOWN 的情况；只按具体反例修原 adapter，不扩大静默恢复认证、不重发旧单。EXPIRED 后迟到 fill 若 native cache 仍为0而业务已为1，仍 HOLD，未假称恢复。D04b真实跨重启与完整启动/停止归 W6；独立的原策略 parity、容量、LONG→LONG→SHORT及残差按 W5继续。W4父项、A07/C07与W5–W9均不因本批绿色而关闭。

2026-09-05 / W5 只读准备与 Q5 原 callee 认证：

- 找到原 ZIP `arbi_main_1_0.zip`，SHA256 `3e50dcc625b1f78048622f3bb5e5ee2f94eb8e4c4635f39108fcb09f6e3d1892`；caller及Bitfinex callee与既有认证值一致，MT5 callee由同一ZIP绑定。不执行原模块/初始化，不复制其中配置或凭据。
- `runtime/q5_margin_oracle_probe.py` 对选定三个纯方法 AST 与成员哈希核验后执行脱敏输入。主 agent 完整读脚本并独立原样复跑：2,000组normalized margin向量，两端4,000个输出零差异；flat/反向持仓/接近margin目标分别为 `(5,5)/(8,2)/(0,8)`。这只认证原callee与向量，不代表现有策略已动态限额或账户真实接口已齐。
- 同时用原Bitfinex `account_info` 生产链验证：反向与接近目标两组一致，但真实flat形态会除零且缺正的持仓成本价。因此下一W5包先明确空仓价格/base margin的迁移政策和精确净仓口径，不能照抄原bug或把补齐后的normalized flat称原式parity。
- 当前Bitfinex已有REST positions查询；下一步先核实其中collateral/collateral_min及既有pl/base_price/leverage如何窄传递到当前AccountState/info，再接原式纯函数与配置上限/挂单/义务占用。MT5现有equity/margin/free可复用，不为此新建风险服务。原caller价格/方向及Maker ask等仓位tie的调查结果留待W5正式脱敏回归接入；当前不扩live多账户。
- 本准备没有修改生产、测试、profile或账户；唯一辅助文件是被忽略的脱敏探针。W5未勾选，现场DEMO与交易未启动。

2026-09-05 / W4c 静默终态及两次独立复核返工完成：

- 主 agent 原样运行15场景探针，确认普通Paper IOC缺自动触发、Maker cancel UNKNOWN不能精确retire，以及原reduce-only静默全成交遗漏raw type/order price/time校验。沿4.4合同修现有adapter，复用原Actor两轮预算，不新增服务、状态文件或任务；足额真实TU已到时不强求再等OC。
- 先前17个普通组合场景全部RED，直接REJECTED另1RED，修后通过。零成交native均价表示与拒单缺venue ID按原生接口做窄适配，不修改cache或伪造accepted。当前协议不产生EXPIRED；防御性的原生EXPIRED后迟到fill恢复仍不冒充已验收。
- 独立review拦下两处原全量绿色未覆盖的漏洞并返工：明确cancel拒绝后新动作继承过期deadline；第一轮native已closed而adapter未retire时，第二轮跳过变化/缺失报告并确认旧事实。修后新撤单使用新期限；每轮重核最新同CID报告和完整真实fills，保留native逐笔核对与已发生的成交/义务。
- 主 agent 新增真实普通双客户端9例：MT5首轮报告不可用，Bitfinex由原生Engine先闭单，下一轮合成REST变更数量/flags或缺失。原REJECTED三负例错误放行，修后保持暂停；不变的REJECTED/partial/full三阳性仍能继续。累计普通Maker/Taker组合35项全部通过，包含实际下一源单SUBMITTED/wire CID绑定、对冲实际量、迟到去重及查询/发单次数；没有手补业务完成状态。MT5 IO仍合成，第二源单未宣称已成交。
- 最终源码/测试未再变化时，主 agent全量 **1312 passed / 80原有Pandas框架弃用警告 / 36.40s**，Ruff全仓、Mypy 71文件及diff-check通过。未参与三文件编写的reviewer独立运行308项回归，并原样复测两处反例及不变报告阳性，给出范围内RECOMMEND-ACCEPT；主 agent据此接受W4c。此包生产码净增63行，修改仍落在原execution，未改Nautilus内核。
- 确认调用约束保持明确：普通runtime必须先看到本轮native reconciliation成功才调用adapter confirm；confirm本身不是可脱离该顺序独立使用的最新报告证明。真实两客户端负例验证该调用顺序，未为防止任意错误调用另造授权状态层。
- 下一停点：先用原生open-order/query只读探针定位尚未发生cancel的Maker漏消息发现能力，两builder目前显式关闭原生open/inflight定时核对，不能把该未覆盖项藏到绿色里；只按证据补最小接线。同时可推进已准备好的W5脱敏callee向量、flat迁移政策和动态容量字段映射，再到连续加仓/反向与残差。D04b、A07/C07及W5–W9仍未关闭，不进入交易。
- 仍为 `codex/audit-remediation` 上以 `7d0d4766c62b98c3e4b950da1d61e789d140e2c5` 为基线的本地候选。已核实每30分钟的 `arbi-nautilus` 为 ACTIVE、旧 `resume-maker-v4-canary` 为 PAUSED；本批没有提交、推送、部署、修改账户/profile或发单。

2026-09-05 / W5a 原容量纯函数完成并接受：

- 新增一个96行的纯计算模块 `margin.py`、对应测试与紧凑脱敏fixture。Bitfinex/MT5均显式返回物理BUY/SELL容量，保留原整数/非整数杠杆、有限负余额及精确Decimal净仓；不访问账户、不增加定时器、不接入发单准入。
- 独立review先确认原ZIP/成员/方法哈希、fixture两千行的28,000个标量与源向量一致，再拦下MT5重复小数杠杆的中间舍入：184资金/4000价格的1oz边界误成0。仅合并分式修正，新增3个参数组RED后转绿；原2,000×两端向量仍零差异。
- 主 agent 独立阅读三文件并运行70项测试；未参与编写的reviewer复测原1,008组整数边界（24处偏差→0）、2,016组两侧与15组负权益，均符合精确有理数结果，给出RECOMMEND-ACCEPT，主 agent接受W5a。Ruff/Mypy通过；生产SHA256 `405fadb686b2c6bc7b84b2e5fa2cf9ac835b8601f39148c6923b1180782a22a6`、测试 `663b0064ca6a67d941713f1f364c12002e132fe7f1c917af6239fcf4492f148b`、fixture `e5ce3f254b1e3891797a6d1cb6ca5e907efe1b4d18f88c3e509235c36d343eaa`。
- W5父项仍未勾选。实际账户完整性/时效、零仓入口、动态容量字段传递与配置/挂单/义务占用的交集尚未接线；有仓字段缺失不能沿用flat值，MT5的BUY/SELL映射不得照搬原duration=-1 tuple顺序。下一包W5b先完成这些既有数据入口的窄接线，再验连续加仓/反向与残差，不新建风险服务。

2026-09-05 / W4d 工作中 Maker 漏消息发现完成并接受：

- 按4.4的实际NT探针结论，继续关闭两builder的原生open/inflight自动checker；只在原Actor/同一client-owned root上增加每5秒的只读active-list观察。精确不变的ACTIVE不查全历史、不撤健康单；差异升为原有两轮完整核对，真实保护性cancel的动作预算独立保留，不靠重复timer盲重试。
- 同run Paper Maker的工作中部分成交与静默终态要求真实trades及raw执行条款，在native应用前验证。REST补成交后再来另一真实TU，native/adapter累计及原义务保持一致；两客户端首轮部分生效后仍核对下一轮最新事实。healthy观察await只暂缓Maker报价刷新，真实公共数据故障/成本到期/业务义务仍能触发原保护动作；canary只兼容该窄回调，不改其profile或交易流程。
- 独立review在1,460项绿色后发现真正REWORK：首轮真实ID A=1oz，次轮累计2oz却只返回新ID B=2oz，native最终False但已累计3oz并新增错误对冲。主agent用普通Maker/双客户端自动路径独立复现，合法A1+B1通过、漏旧ID失败；修复仅在既有完整真实成交证明边界增加9行集合覆盖检查，旧ID的精确映射沿用现有逐项核验，不影响active-list空fills或原inferred/cold范围。
- 修后原独立探针原样exit0，漏旧ID在交Engine前拒绝，native/live仍1oz、原事件和义务完全不变。实施者新增WS/REST首笔×完整/漏旧/篡改6例（2RED→6GREEN）；主agent新增普通路径2例（旧版本1RED/1GREEN→2GREEN）。后者等待首轮真实保护性cancel的实际期限在本轮内到达后断言预算不重开，没有修改生产期限或以手补cache/store完成业务。
- 普通Maker/Taker组合现62项通过；本包新增27项覆盖主动发现、缺失/冲突证据、健康观察与真实故障、REST/WS交错、双客户端重试及完整累计集合。原35项连续性用例保持；新的4oz只为离线反例夹具，真实profile/累计额度未变。下一源单的SUBMITTED/wire绑定不冒称第二次venue成交，MT5 wire仍合成，LONG/SHORT完整连续矩阵留W5。
- 非超时目标的业务确认测试仍真实落盘；其30ms期限受fsync调度影响，故只把该夹具设为300ms，生产与专用超时测试不变。40ms真实fsync后延迟可复现旧断言失败，且成功回调并未重复；没有原始历史超时日志，不宣称已证明历史偶发的唯一根因。
- 未参与这些代码编写的reviewer复跑原探针、458项相关回归及root新增普通2例，结合前轮单root/预算/停止/健康观察的只读核验，给出范围内RECOMMEND-ACCEPT；主agent接受W4d。最终冻结代码上主agent全量 **1468 passed / 80既有Pandas框架弃用警告 / 58.91s**，Ruff全仓、Mypy 73文件与diff-check通过。execution SHA256 `82eb40e29c2ad9d49b4991c632c7ad1dcb4d508291fb3d4bf06505a090ab8d6d`、runtime `c60c93442824c8d394f8f455c39340397abff9c95a4d8cd2d30c4b96c69d7c4d`、普通组合测试 `a9c87f9e3819d773d408075d504c5fd0c4d4f7344c3a5d01f541c00c46dc9662`。
- 分支/HEAD仍为 `codex/audit-remediation @ 7d0d4766c62b98c3e4b950da1d61e789d140e2c5` 的本地未提交候选，没有推送、部署或发单。当前复查发现 `arbi-nautilus` 已变为PAUSED（`updated_at=1788611405664`），本轮未修改或重新启用它；旧canary也保持PAUSED。本次用户“继续”的当前轮实施正常收尾，不承诺暂停期间自动续跑。W4父项、W5动态接线以及W6–W9仍未关闭。

2026-09-05 / 按步提交的检查点：用户要求后续按小步提交并继续。主agent核对全部49条变更路径、已有独立验收及冻结源码，重新全量1468 passed（80既有warnings，62.41s）、Ruff、Mypy 73文件与diff-check通过后，提交此前累积的相互依赖修复为`f1d7cdcb7d15f34719eda3054158778f143de5a0`，提交后工作区干净。该提交是已记录范围的基础检查点，不代表W1–W9全部关闭；没有推送或部署。下一提交从上面的W5b1账户事实入口开始。

2026-09-05 / W5b1账户事实入口完成并接受：

- BFX复用现有PositionState、私有消息与REST报告入口，把钱包/持仓的原始值、完整性和各自观察时间作为完整AccountState.info发布；保留available=null，不改变原Money表示。MT5沿已有完整snapshot发布精确净盎司、票数及独立账户时间，并增加读取时的窄年龄资格。两端原成交、对冲、UNKNOWN、foreign-magic和执行准入保持；没有新服务或状态文件。
- 已应用native fill使旧样本失效，不以fee CID绑定作前置；重复成交或费用历史扫描不再次失效。REST读取跨越新PS、fill或连接变更时不能把旧结果装成当前样本。MT5未来/倒序不污染合法时间水位，同秒账户变化可发布；旧AccountState及嵌套info不被后续发布原地修改。
- 独立审核拦下串联伪flat：PS44后出现含混PS44/99或未知新ID，随后PC44把未解释的99错误排除；REST enrichment失败有同根遗漏。主agent在普通Maker/Taker各新增一条实际Account/Portfolio路径反例，两条先RED。最小修复撤销失败投影的complete/current，保留last-good；后续delta不能自证完整，必须合法PS/REST。实施者补18条失败入口×后续delta及1条迟到REST错误竞争；现均通过。原报告mapper自身明确拒绝也按同一规则处理，但原错误照常抛出，不扩大timeout/cancel分类。
- 首轮全量1518过/2失败，另与上述生产缺陷无关的根因是同进程回测永久注册Nautilus calculated-account模式，框架随后用空info覆盖报告账户。主agent和独立审核分别以普通回测、Paper canary作为最小前置复现，两例单跑均绿。仅在普通测试harness启动前显式构造原生非推算MarginAccount，隔离回测注册；保留真实WS/PS/fill到最新account.last_event的断言，不篡改原生全局注册或运行中cache/store。两种前置修后通过，Paper前置加原70项普通组合共71过。
- 相对检查点新增73个案例（BFX44、MT5 19、普通组合10），普通组合共72项；生产净增234行，没有新生产模块。最终冻结版主agent全量 **1541 passed / 80既有Pandas框架弃用警告 / 63.06s**，Ruff全仓、Mypy 73文件与diff-check通过。BFX execution SHA256 `647943e59a20036e39dcec9931f14477f066328cbca0197ad56f704946fb945d`，MT5 execution `e8ffc2646a627fc46285a03c420e01630b3887be40dc880bb47b52aed0a75f66`，普通组合测试 `c530b1324032bcf862228607575ce46d42df3259d2e7a85478d67d6201e961d8`。
- 未参与本包编码的reviewer重新核对全部7文件冻结哈希，复跑原PS/PN/PU/REST伪flat探针及4入口×2完整恢复路径共8组，确认拒绝后last-good不变、旧PC不能造flat、合法PS/REST恢复后PU/PC/新PN仍正常。另独立BFX389项、普通账户10项及MT5先前135+4项通过，Ruff/Mypy/diff-check通过，关闭原R1并给出RECOMMEND-ACCEPT；主agent接受W5b1。全量1541为主agent执行，不冒称reviewer重复执行了全量。
- 本步只完成事实入口，不把current/sample_valid当最终下单许可。下一小步先接已有native QueryAccount的按需、有界刷新，证明钱包/持仓联合采样无fill/连接竞争，再把margin.py与动态杠杆接入既有两策略账户视图；挂单/义务占用、降风险/穿零和连续仓位仍分别验证。未修改profile、EA、真实账户或交易额度，未推送、部署、发单；W5父项及W6–W9仍未关闭。

2026-09-05 / W5b2按需账户刷新完成并接受（基线`6640516`）：

- 补齐Bitfinex原生QueryAccount入口：一个强引用在途task，重复命令不增加读取；钱包、持仓分别记读取时间，验证和Money构造完成后一次AccountState发布。两读取及REST锁等待共用既有超时预算；目标账户/连接不合格仅日志返回。无额外轮询器、状态文件或生产模块，生产净增106行；MT5原1秒完整刷新未改。
- 公共入口在排task前捕获持仓/钱包两个水位；同值WU、PS、真实fill、净量往返或连接切换均能废弃旧候选。disconnect和reader故障取消并等待账户task；普通Maker/Taker的真实成交仍进入原对冲路径。原独立positions REST不因钱包变化误失效。
- 原入口缺少私有实现，实施者首20项实跑RED后修复；主agent普通原生入口首例进程exit1，并用独立直调定位AttributeError，不把无pytest汇总的退出冒称6项失败。首版全量1576通过后，独立复核仍发现两处Decimal算术异常未撤旧资格：真实REST解码的有限`1e1000000`在钱包减法/持仓abs中溢出。direct两项及普通两阶段×两策略四项分别先RED；仅新query两个候选catch覆盖ArithmeticError后转绿，原异常继续传播、last-good值/时间不动，仅撤对应资格。不扩大旧报告/REST/reader的错误分类。
- 最终新增41项（BFX31、普通组合10）。主agent全量 **1582 passed / 80既有Pandas框架弃用警告 / 62.25s**，普通native query另跑10过，Ruff全仓、配置化Mypy 73文件和diff-check通过。独立reviewer重放两条真实REST反例及显式恢复，并核验锁排队/持锁HTTP取消、迟到抛错不撤新样本；其BFX execution/engine/protocol/rest四文件462过、普通query10过，Ruff/Mypy/diff-check通过，给出RECOMMEND-ACCEPT，主agent接受。最终生产SHA256为`508df45dca6c1fcf54a0f42f9e5eac1e977d3d5b6e66dc143ccd53555684d59a`。
- 本步只交付可调用的事实刷新，不代表策略已自动请求账户或使用动态容量。下一步在既有账户视图中接margin.py、动态杠杆和按需触发，再分别验证挂单/义务占用及降风险/穿零。按用户要求独立本地提交；未推送、部署、实读账户或发单，W5父项及W6–W9仍未完成。

2026-09-06 / W5b3账户事实到动态容量视图完成并接受（基线`a0c4dc8`）：

- 在既有`margin.py`实现两端原生AccountState到三个既有账户record的纯映射，生产净增235行，没有新模块、模型、服务或状态文件。原70项normalized算式测试保持。BFX有仓的动态base margin同时传给原下单杠杆计算，完整flat才采用显式route base和新鲜ask；MT5保留原base60。两端均取动态容量、route方向容量、方向净仓空间交集，不重复减净量。
- 样本缺失、错身份/计价币种、过期/未来、非完整持仓和不可计算数值返回None，不补零仓或静态/无限容量，不修改历史事件。当前客户端资格是必填事实；实际MT5 fill撤销内部资格后，即使旧event.info仍valid，映射也不可用。独立复核拦下单票净零和EUR/None本位币反例，分别2项和6项先RED后GREEN；多票净零、合法币种及精确小数仓位保持。
- 新增170项：161项映射单测，8项普通Maker/Taker真实AccountState到映射再到原经济函数的离线场景，1项实际MT5 adapter fill资格回归。覆盖Taker缩量、Maker固定数量不足时不报价、动态杠杆，以及正反向已有12/限10仍减至11或穿零至另一侧10，不能穿至11/12。这8项普通组合验证经济产出，不声称策略已自动调用映射；MT5 fill项为组件级，不冒充完整策略运行或EA实测。
- 首轮全量1751过/1红来自既有LiveClock测试的恰好一次回调断言；按上文有界诊断实证早醒后正常重排。只修测试断言，仍严查正确loop、恰好两次持久化和两张单各撤一次，主agent及独立reviewer各跑8组合通过。该独立test-only前置已本地提交`bcfc509`，没有修改生产定时器、期限或冻结逻辑。
- 最终冻结候选上主agent全量 **1752 passed / 80既有Pandas框架弃用警告 / 61.53s**，Ruff全仓、Mypy 73文件和diff-check通过。未参与本包编码的reviewer核验450组独立Fraction算式（BFX360、MT590）、容量及两套economics共263项、普通/组件新增9项及三份冻结SHA，给出本包RECOMMEND-ACCEPT；主agent结合测试前置独立复核和最终全量接受W5b3。全量1752为主agent执行，不冒称reviewer重复全量。生产SHA256 `8cd63d8b22e74dfdf31da07af828ac0d53738d4f103ea8508ec6122b3ec993a6`，映射测试`9d7fe92437e33d21f9a2ea02f8fab8607d1f608040affa40c1f813946196ac1a`，普通组合测试`b65f9fde11c0fc9803a9074f753a8c156497b29db31f8523c44baf2652608f3c`。
- 下一包把策略自动视图、按需query触发/节流与Maker自身、双边、partial和pending-cancel占用一起接入；先核实venue可用资金与工作单占用的口径，避免自撤循环或同一风险重复扣除，不用新开关绕过动态检查。本包未改策略、builder、runtime、adapter、profile或EA，未推送、部署、访问真实账户或发单；W5父项、原子残差和W6–W9仍未完成。

2026-09-06 / W5b4普通策略自动动态容量接线完成并接受（基线`6271676`）：

- 两live builder强制绑定同一账户reader，经原生AccountState及当前client资格调用W5b3映射，再进入原Maker/Taker经济计算；不增加开关或live静态fallback。BFX缺事实、接近TTL或需操作后预算时按需调用native QueryAccount，保留2秒最小间隔、client单task/超时/取消；MT5沿既有刷新。生产净增476行，均在7个既有模块，没有新服务、配置或状态文件。
- BFX增加窄action水位，操作前开始、ACK后才返回的query不能认证新预算；同回调首单的native INITIALIZED也立即阻止另一侧复用旧预算。先9项真实引擎查询竞争RED，再覆盖24项预算回归；独立review另以实际ExecutionEngine命令及barrier核对submit/modify/cancel、UNKNOWN和重复报告，不靠返回时间证明因果。
- 新source用完整动态额度；已有Maker不以剩余free重新申请整笔旧量，但精确native leaves、route/net risk、MT5容量及账户时效仍有效。等待新侧预算时暂停健康旧单改价，账户恢复后可发另一侧；旧单price+lev所需额外抵押仍由venue裁决，不建立猜测性的锁资账。账户通知只合并延后到原loop，保持真实行情时间、Taker去重及canary覆写；stop退订/取消回调并隔离旧代际，Maker原timer覆盖账户最早到期。
- 主agent普通入口首5项语义RED修后通过；最终新增7项真实普通组合证明冷启动主动查询、无新行情账户恢复、Taker实际缩量、Maker固定量不足拒发、旧单free下降保留/MT5容量下降撤单、双边8次价变期间不饿死及账户独立TTL撤单。旧测试补完整模拟venue事实、更新MT5快照和当前carry，不修改生产节流/时效、不手补业务完成状态；账户恢复可自动发第二单，核对仍以真实native/wire/CID/数量为准。
- 普通组合发现并修复真实回归：账户回调先保护cancel，REST partial fill把native状态改回PARTIALLY_FILLED，旧代码重复本地cancel并形成reject/HOLD链。主agent、实施者与reviewer分别复现；仅复用native事件历史的7行判断修复，不放宽旧终态证明或按reason吞拒绝。新增3项先RED后GREEN，原3条普通失败流程现在首CID仅1条native cancel/1条wire OC、无重复拒绝，真实fill/hedge和下一源单完整；明确拒绝仍HOLD。
- 相对基线新增56项（BFX24、策略事件22、runtime3、普通组合7）。最终主agent全量 **1808 passed / 84 warnings / 59.49s**，Ruff全仓、Mypy 73文件及diff-check通过，运行前后13份源码/测试SHA完全相同。未参与编码的reviewer独立345项及真实嵌套fill/查询竞争探针通过，关闭重复撤单REWORK并给出RECOMMEND-ACCEPT；主agent据此接受本包。全量1808为主agent证据，不冒称reviewer重复全量。
- 按约定形成本地小包提交；不推送、部署、改profile/EA、访问真实账户或发单。下一包先完成正常策略的连续加仓/反向/穿零矩阵，再推进已定的原子残差；W5父项、W6–W9和真实DEMO连续验收仍未完成，不能将本包绿色称为可上线。

2026-09-06 / W5b5连续仓位与提交前票据一致性完成并接受（基线`1883b9a`）：

- 首轮执行边界25项为23 RED/2 GREEN：旧adapter放过已计划目标数量变化、marked open不强刷及坏前提参数。普通Maker/Taker到真实MT5 adapter的8项全部RED，实际native SubmitOrder.params为空；此前模拟身份不匹配的夹具错误不算目标反例。修复仅4个既有生产模块，净增57行；`hedge_order_params`携带`py000_hedge_plan=True`和close的有限正Decimal预期票量，沿原生路由传递。adapter在原锁内强刷，close比较整张目标票量（不是把本腿减仓量当整票量），open拒绝反向票；原无标记手动命令语义、exact-close/UNKNOWN保护及EA wire不变。
- 新增16项普通原生Backtest矩阵：Maker/Taker、LONG/SHORT对称，覆盖2→4→2、部分反向、穿零及1+2两票遇delta4的close1→close2→open1。启动配置不变，以真实L2盘口/行情撮合；只读观察每腿提交前所有前腿已原生全成交，逐阶段核对source/hedge净仓、实际PositionId、reduce_only、数量及完成义务，未手改仓位或每轮清仓。Maker Backtest缺live REST入口，明确用实际native closed order构造终态报告经原callback核验；这是模拟撮合读回，不冒充真实venue对账。初稿缺此读回导致Maker周期停住，以及同时间CID字符串排序把10排在9前，均为测试承载问题，未据此修改生产生命周期。
- 新增30项adapter边界（25新例及5个既有invalid-target的marked扩参），另20项普通builder→两真实adapter→原生Engine的exact/drift/pending/rejected/unknown序列。后者仅替换venue transport响应，初始反向仓通过真实native订单建立，source由原机会入口产生；不使用旧append-only MT5提交替身。新快照不一致时零mutation；在途、拒绝和未知时真实行情也不触发余腿或新源。UNKNOWN的业务义务保留SUBMITTED未完成且adapter持有unknown，不伪造COMPLETED或把残量清零。无关mass reports及所有venue IO仍是synthetic，未执行EA。
- 主agent冻结候选全量 **1874 passed / 124条框架弃用警告 / 65.61s**，Ruff全仓、Mypy 74文件和diff-check通过；相对基线新增66项，8份生产/测试SHA运行前后完全一致。实施者另跑相关五文件508项通过。未参与本包编码的reviewer独立四文件 **330 passed / 43 warnings / 38.13s**（此前71定向为其子集，不累加），另3个BUY镜像探针通过；只在内存中关闭新前提后，Maker/Taker×open/close的4个原ordinary漂移案例均在预期DENIED断言重新RED，恢复冻结实现均GREEN。reviewer核验8份最终SHA后给出RECOMMEND-ACCEPT，主agent据独立语义复核及最终全量仅接受本小步；不把focused或独立复核冒称第二次全量。
- 按约定形成本地提交。完整双adapter连续多票矩阵、Maker原子残差/strict/bounded-carry、W6–W9及W5父项仍未完成；snapshot读取与EA mutation之间的外部竞态不据本包消除，多写者串行归W7。不推送、部署、改profile/EA、访问真实账户或发单。

2026-09-06 / W5b6双adapter连续成交闭环完成并接受（基线`dfa90fb`）：

- 新增24项普通双execution adapter矩阵：8项Maker/Taker与LONG/SHORT对称的2→4→2→1→反向1、1→3→反向1连续路径，另16项腿间pending/drift/rejected/unknown。原builder、策略、native Engine和source-terminal owner保持；MT5以原public connect启动原poller，策略通过实际新行情与原账户刷新进入下一轮。启动配置固定、0.02lot单腿上限保持，不通过清仓、重启、改业务状态或降低节流/时效完成周期；最后非零库存上仍有下一原生源单实际ACCEPTED。
- 两端执行IO只由有限测试事实响应：MT5新增162行wire及4项自检，positions随open/partial-close/exact-close更新，同一完整journal可重复读取；Bitfinex的on/ou/oc、真实有符号TU与REST读取相同订单/成交/净仓，均价同向加权、减仓保持、穿零重置。逐阶段对照实际source fill、native双端净仓、ticket/identifier、reduce_only、单腿量及两原adapter mass内容；每条venue mutation前只读验证前腿native已FILLED。不是网络、EA或PnL模拟平台。
- 联测发现的两处夹具不足已纠正：初稿仅调用MT5 `_connect`未启动原poller，恰被手动mass读取刷新掩盖，现必须原poller先提供account_capacity_ready，fault setup连续1→3不调用mass也可进下一单；持久JSON按key排序后，重载义务必须按intent_id比较，不能把遍历顺序差异当生产丢失。未据这些测试承载问题修改生产。
- 生产只改既有Maker文件，净增24行，无新状态、adapter/EA或协议改动。真实WS fill与已排队改价交错时，native的closed/pending-cancel拒绝曾无条件把已知源事实降为UNKNOWN并留下永久halt；现复用原身份、路由、数量、逐笔seen-fill证明，区分精确终态和已冻结partial的未决保护取消。新52项Maker参数回归：终态分支28项先11RED/17GREEN；partial分支20项先4RED/16GREEN；审核追加4项先4RED。真实取消拒绝、UNKNOWN、矛盾事实及旧HOLD仍保留，取消未决不等于终态，更不等于对冲完成。
- 独立审核拦下初版历史扫描遗漏：真实Canceled→late PartialFill可使当前native再次partial，必须用历史Canceled等完成事实结束旧取消；不能跳过终态而复活旧PendingCancel。审核者原样原生Engine探针修前RED、修后pending/proof均False；真正新PendingCancel后两者又为True，modify拒绝不改store字节，两笔未完成义务和net_unhedged=2继续阻断新源。Expired仅验证终态结束取消，不外推其迟到partial转换。旧全量候选因此主动终止，未计为通过。
- 最终冻结上主agent重新全量 **1954 passed / 124既有Pandas框架弃用警告 / 232.05s**，Ruff全仓、Mypy76文件、diff-check通过；6份源码/测试SHA运行前后相同。相对基线新增80项（矩阵24、MT5自检4、Maker52）。未参与编码的reviewer独立五文件 **535 passed / 6 warnings / 206.95s**，含最终24矩阵，不累加此前子集；再次核验六份SHA及上述反例后给出RECOMMEND-ACCEPT，主agent接受本包。1954全量为主agent证据，不冒称reviewer重复全量。
- 按约定形成本地提交，不推送、部署、改profile/EA、访问真实账户或发单。UNKNOWN实测仍是adapter pending保留、mass返回None、义务未完成，不是空仓或必然断线。仅关闭上述普通离线连续范围；实际PUB/data decoder、网络/EA/DEMO、snapshot→EA竞态未认证，W5父项、原子残差、剩余parity和W6–W9继续未完。下一包按既定4.5先做Maker原子残差与strict/bounded-carry，不以本包绿色宣布上线。

2026-09-06 / W5c1 Maker单文件状态边界完成并接受（基线`c7a8698`）：

- 新增232行Maker owner/view，复用原v1订单、fill/intent和多腿算法，仅从`store.py`抽出原JSON解析与写入。`<prefix>.maker.json`显式schema v2绑定两个品种，bid/ask由同一写入保存；实际fill与双freeze同次提交、双release保持原证据门且同次提交。替换前失败整份回滚，替换后父目录同步失败整份保留并上抛；两既存view与reload一致，已提交义务不因随后写失败而丢失。未实现净额或carry，±0.4/±0.5仍分别保留并strict HOLD。
- 普通Maker、离线replay、live builder与canary路径识别均接通；CID/new/legacy/transcript碰撞、旧文件存在时拒绝空启动、新文件存在时不重导旧文件都有测试。旧文件不改不删，v1 reader拒绝v2；源/对冲身份和同账户trade/deal不能跨view重复认领。旧双文件的显式转换工具尚未交付，不能将此提交直接用于旧状态在线升级。
- 主agent保留旧实现6条语义RED：两view不同文件、首fill快照缺双freeze、release分两次写，以及新path的CID/输出碰撞和canary漏检。新store42项覆盖原子故障、重载、身份/格式和strict残差。旧私有corruption负例仍先证明obsolete proof=False；非法身份/删除seen-fill的7例现在拒绝写坏快照，回到last-good并保留HOLD和义务，其它负例继续要求UNKNOWN。全局flight夹具改为先有双工作单再交错fill，不绕过新冻结门。
- 首次全量 **5 failed / 2002 passed / 124 warnings / 233.61s**，独立审核同样复现5 RED：两处旧fixture的fill key尾段与trade_id字段不一致。只修正两处fixture身份，不改路由拒绝、cleanup、多腿或其它断言；未把该失败全量计为通过。新候选相关六文件296项通过，普通/双adapter连续142项先前通过。
- 最终主agent重新全量 **2007 passed / 124既有框架弃用警告 / 233.47s**；全仓Ruff、Mypy78文件及diff-check通过，12份源码/测试SHA测前测后相同。独立reviewer在最终两test SHA上原样七文件 **349 passed / 8 warnings / 5.31s**，另实跑post-rename后另一view pre-rename失败、原Maker fill callback post-rename失败并后续只推进一次义务两探针；复核12份SHA后RECOMMEND-ACCEPT。主agent据此接受W5c1，不将独立子集与全量累计。
- 按约定本地提交，不推送、部署、改EA/profile、访问真实账户或发单。下一包沿4.5实现同route先合并再舍入的原子残差分配及有界carry，并完成显式旧状态迁移；W5父项、E04/E06完整迁移/E09、剩余parity及W6–W9仍未关闭，不据本包宣布上线。

2026-09-06 / W5c2 strict 同 route 原子残差净额完成并接受（基线`1e902bc`）：

- 三个既有生产模块净增156行；原`reserve_source_fill`仅增加提交前分配hook，Maker schema v3保存有序真实fill key、signed数量、route快照和实际signed分配。残差由该列表唯一导出，不另存第二份余额；原源累计、seen、intent和双freeze同次写。各view只投影route最后真实fill方向，未分配残差先合并再调用原舍入；已有PENDING/SUBMITTED义务不被反向fill擦除。四个account/client、None精确匹配、缺账户或特定平仓票据的CID隔离，以及同一view投影合计0但不同route分别±0.4仍strict HOLD均有实际测试。
- 修前保留8项旧store业务RED和4项原生事件业务RED：同route±0.5仍分别留存、+0.5后-0.6错出1oz对冲及镜像。新增40项Maker状态、4项原生事件、4项普通双adapter集成，共48项。普通Maker经原经济函数先挂出两张真实native订单，venue fixture在保护撤单到达前产生双边真实WS fill；±0.5经原双cancel及完整mass核对后，下一普通经济2oz订单正常进入原MT5流程，最终source/hedge净仓为±2/∓2；±0.1 dust镜像仍持久HOLD，不靠私改store、清仓或canary覆盖来继续。
- 测试承载错误未当业务证据：原生初稿的整数精度instrument无法生成0.5，改为合法小数CryptoPerpetual后才得到上述4RED；普通集成初稿误把MT5的`stream_started`算为订单活动，现保留完整初始journal并严格要求净额阶段无新增、下一真实hedge仅新增reserved/filled。旧private corruption里修改累计或四route的12例现在违反真实ledger，保留原故障和obsolete proof=False，要求拒写、last-good、已知义务及reload HOLD；其它负例仍要求UNKNOWN。未放宽生产校验或删除反例。
- 新reader明确拒绝v2和旧双v1，不改不删旧文件。旧v2接口的(0.1,0.4)和(0.2,0.3)同CID/两fill key历史产生相同快照，证明累计/seen不能一般性恢复逐笔量；新v3在相同direction累计下保留不同真实allocation记录。显式旧迁移后续选择真实补证或清楚标识的legacy汇总起点，不能重跑新算法改写历史义务或清UNKNOWN。
- 最终冻结上主agent全量 **2055 passed / 124既有Pandas框架弃用警告 / 248.88s**；全仓Ruff、Mypy78文件、diff-check通过，六份源码/测试SHA测前测后相同。独立reviewer九文件 **499 passed / 77 warnings / 5.79s**，另普通双adapter新矩阵 **4 passed / 16.62s**；仅在内存关闭共享池后原生4例全部按原业务断言重新RED，恢复实现GREEN。其串联实际故障探针确认跨方向净额post-rename失败保留全新，再遇后续pre-rename失败不回退已提交ledger，重放不重复分配。核验六SHA后给出RECOMMEND-ACCEPT；主agent据全量与独立语义证据接受本小步，子集不与全量累计。
- 按约定本地提交，不推送、部署、改EA/profile、访问真实账户或发单。仅关闭E04/E05与E06的本格式原子写部分；E06旧迁移、E09有界carry、W5父项及W6–W9仍未关闭，不能用于直接升级旧状态或宣称上线。下一独立包先固定显式迁移的历史起点规则，再接4.5的carry预算与跨cycle验收；不静默增加在线残差额度。

2026-09-06 / W5c3 显式旧状态迁移完成并接受（基线`97489a5`）：

- 完整双v1/单v2经旧direction守恒与历史事实检查，生成不同prefix的schema v4。checkpoint只固定真实累计、旧fill/intent身份及已分配量；原订单、计划、去重、UNKNOWN/HOLD保留，之后只有新真实fill进入allocation。不复跑历史舍入、不自动补单；旧v3读写不变，v2有头品种必须吻合，v1品种明确为操作人绑定声明。
- 新文件用同目录完整临时件原子no-clobber发布，输入变化、缺方向、冲突目标及发布前失败拒绝且留原件；发布后父同步失败留完整新件。独立复核补充并修复临时名清理误报：已创建并同步成功后unlink失败只告警，真实CLI反例修前返回1、修后成功且结果可重载、重试仍拒绝覆盖；不吞发布/同步异常。
- 新增43项：34项迁移、7项发布原语/原store、2项普通双adapter。跨route余额±0.4即使旧hedge全部COMPLETED、view合计0仍strict HOLD；已知单笔量大于累计、旧/新key交叠及绑定变更拒绝。普通Maker从两种旧格式启动后，原经济判断产生真实native新源2oz，经原Bitfinex/MT5执行路径对冲−2oz；保留旧字节与checkpoint，只增新真实allocation，不用私改store或重放旧单促成续跑。
- 首冻结全量2097通过后追加上述清理修正；最终候选重新全量 **2098 passed / 124既有Pandas框架弃用警告 / 251.74s**，全仓Ruff、Mypy80文件、diff-check通过。独立reviewer最终八文件 **386 passed / 4 warnings / 3.43s**，另普通双adapter **2 passed / 3.49s**；额外实跑两线程同目标恰一成功一FileExistsError，以及历史±0.5抵消后去重/新fill/重载探针。核验九份代码/测试/入口SHA后RECOMMEND-ACCEPT，主agent据新全量与独立证据接受本小步；子集不与全量累计。
- 按约定本地提交，不推送、部署、改EA/profile、访问真实账户或发单。E06显式转换部分关闭；不认证在场writer互斥、冷cache恢复或线上升级。E09 bounded-carry、W5剩余parity及W6–W9仍未完，下一步继续离线残差预算与跨cycle行为，不因此提高在线额度。

2026-09-06 / W5c4a Maker跨方向对冲有序执行完成并接受（基线`710b1be`）：

- 预审发现原调度把真实交错义务按bid/ask重排：双源各2oz内的BUY0.6/SELL0.2/BUY0.2等七次实际fill，使原selector先SELL四次再BUY三次，MT5中途净仓-4、未对冲-3.4。该反例由独立reviewer实际store/selector探针确认；主agent普通双adapter两镜像、实施agent真实Engine源事件两镜像均先在原行为上RED，失败是确切次序错误，不是接口缺失或fixture启动失败。
- 生产净增11行：owner增加从现有allocations和intent表取首个未完成义务的只读视图，策略替换原bid/ask选择；无第二队列、缓存账或schema变化。零分配/已完成略过，多腿全部完成再下一intent；原全局flight/UNKNOWN/REJECTED/BLOCKED门不变。旧checkpoint任一未决义务阻挡后续，不能猜其历史顺序；已完成旧义务保留，不影响新allocation按真实顺序推进。
- 新增23项：21项owner/原生事件及2项普通双adapter。后者原经济报价生成两工作单、第一MT5实际请求保持在途、其余源TU在保护cancel送达前交错入账，再沿原协议/journal/native/多票执行；每笔真实MT5后净仓仅在0/±1间，最终±0.4 dust仍strict HOLD，完整报告与native一致。两处旧quote-retry私harness改用当前owner并修正其fill key/trade ID不一致，未改原断言或放宽生产校验。
- 最终五份源码/测试SHA上主agent全量 **2121 passed / 124既有Pandas框架弃用警告 / 258.82s**，全仓Ruff、Mypy80文件、diff-check通过。独立reviewer五文件 **340 passed / 4 warnings / 2.29s**，普通双adapter **2 passed / 7.41s**；原selector探针镜像通过，内存反事实恢复bid/ask选择使原生两例重新精确RED。核验五SHA后给出RECOMMEND-ACCEPT；主agent据全量与上述证据接受本小步，不累加子集。
- 一次额外adapter子集为31 passed/1 timeout，明确受到主agent在运行中发出的Ctrl-C干预，不记成自然回归或通过。工具独立实测确认非TTY的该操作也发送SIGINT；固定NT源码注册信号并stop当前node而非退出pytest，与失败第五cycle的02:00:58时间相符。主agent随后仅对自身隔离测试进程发SIGINT，原第五cycle再次出现全部义务完成/owner无错但node与MT5断开、原8秒条件超时（21.64s探针通过）；无中断的该case17.14s通过。临时诊断字段已撤回，最终五SHA与全量/独立审核一致，不修改timeout、就绪门或adapter来消除这项人为失败。
- 按约定本地单独提交，不推送、部署、改EA/profile、访问账户或发单。W5c4b的单/双侧最坏敞口、分片累计净delta与单intent/lot分别验量规则已固定但尚未实现；E09、剩余parity、W6–W9仍未关闭。此次信号诊断只解释受干预测试，不把完整启停/drain认证提前计入W6。

2026-09-06 / W5c4b 单Maker有界残差跨cycle完成并接受（基线`9bd9e5e`）：

- 五个既有生产模块净增231行：配置显式选择strict或bounded-carry；复用原ledger与小余额资格hook，无新schema、余额账、执行层或持久队列。默认strict/线上profile/Taker不放宽；一个明确account+client route才可使用残差额度，异route、UNKNOWN、未完成义务及原HOLD保留。双终态release与下一cycle第二侧准入分开，逐intent真实剩余不能被正负净和0掩盖。
- 准入以native规范化后的实际BUY/SELL leaves分别计算，维护精确排除自身CID且复核原数量；沿原动态容量/净仓规则查累计delta，再独立检查每intent、初始各票及未来open的保守单腿上界与1oz可执行单位。预算或回调失败拒新源/撤旧报价，已成交义务继续原有序对冲；不把累计3oz误作单腿3oz，也不遗漏票据演化后实际可能的open3/close3。
- 原strict dust双adapter2例先实测仍HOLD；新config接口先RED。首轮新普通10例9过1失败，真实零fill对侧PendingUpdate→PendingCancel→ModifyRejected留下永久halt。按上文窄合同补原proof，原生确定性RED及负对照通过，原普通失败case单独回验通过；未改deadline或排空竞态。维护helper精确CID但候选1oz/native leaves2oz的反例也先RED后窄修，不低估占用。
- 新增70项测试，其中12项为普通Maker经两个真实execution adapter及合成venue IO：±0.5带入下一轮双侧经济报价；更小限额/预算阻挡；2oz分成0.2+1.8真实对冲1+2，留下反号0.5；实际先形成票据再带dust时阻挡未来open3；原MT5 poller读取equity下降后撤销累计容量不足的工作报价。仓位、trade、journal、native报告和重载余额一致；stop保留真实signed dust且无自动平仓交易。
- 最终十份代码/测试SHA上主agent单次全量 **2191 passed / 124既有Pandas框架弃用警告 / 292.42s**，全仓Ruff、Mypy81文件与diff-check通过。独立reviewer核心六文件 **404 passed / 2 warnings / 2.66s**，普通双adapter **12 passed / 35.40s**；原舍入/保守界的84,503个有限分片转换枚举通过，并确认余额release不绕过更小max_unhedged。十SHA前后相同，独立结论RECOMMEND-ACCEPT，主agent据新全量和独立证据接受本包；子集不累加为全量数量。
- 按约定单独本地提交，不推送、部署、改EA/profile、访问真实账户或发单。仅关闭E09的单Maker离线carry/数量报告范围；不认证真实PUB/EA/DEMO、冷cache重启、完整drain、共账户额度或外部snapshot→mutation竞态。下一步收齐W5剩余原策略parity，再依既定Q3/W6推进恢复与停止；W5父项及W6–W9继续未关闭。

2026-09-06 / W5d 原策略规范化经济对照完成并接受（基线`228f5c1`）：

- 生产只改既有 Maker 两个选择器、净增2行：source固定升序、hedge固定降序，bid正向/ask反向取首个合格候选，含稳定排序tie；资格、carry预算、默认strict、Taker和adapter均不改。新测试在旧代码先得到4个身份业务RED（双tie、单source、单hedge、末个tie不合格），修后通过；多账户向量只认证既有纯helper，不扩展普通live的一对账户支持。
- 两份紧凑fixture共91例：59个caller（Taker33/Maker26），17个规范化POINTS、11个明确差异及4个同值funding getter。主agent与独立reviewer均实际认证原ZIP/成员/选定方法并重跑全部原输出MATCH；只执行选定AST和合成依赖，不导入原模块或运行配置/初始化。caller净收益来自原函数return frame的只读局部值观测；原浮点输出保留，数值容差不用于方向、身份或风险布尔值。CI新增测试不读取本机ZIP，两个复跑探针保留在ignored runtime，不复制原载体。
- 严格阈值使用精确`144/128-1=0.125`及相邻threshold；short-first、拒绝后不退LONG、四项数量交集、降风险例外、穿零/minimum、Maker signed量和leverage均有原输出对照。POINTS显式旧0→native1，原pytz时区及完整星期字典对当前七项向量；缺数据拒绝、原wire编号未认证、BFX `margin_funding`与`NEXT_FUNDING_ACCRUED`不同producer、FX显式预映射及Maker终价Owner冻结分别保留，不包装成原始全链parity或实收现金流。
- 新增101项测试。真实Bitfinex data parser→FundingRateUpdate→DataEngine→两策略回调覆盖方向、fee/FX、Instrument更新不刷新funding及独立过期/恢复；夏冬四例经真实quote回调使用决策日三倍而非旧quote日正常倍率。该Maker跨日场景显式源配置量0，只认证真实经济回调，不冒称原生发单验收；正常TTL未放宽，transport未连接。
- 冻结五份源码/测试/fixture及两份本机probe上，主agent单次全量 **2292 passed / 129既有类别Pandas弃用警告 / 291.60s**，全仓Ruff、Mypy83文件与diff-check通过；七SHA测前测后不变。独立reviewer成本40项、caller/经济117项通过；仅在内存恢复旧source选择器使单端tie重新RED，仅改用quote时间使跨日成本从`-0.0000945`错为`-0.0000315`也重新RED，恢复后原样正常。其核验七SHA后RECOMMEND-ACCEPT；主agent据新全量与独立证据接受本包，子集不累加为全量数量。
- W3/C07及W5/E08仅在上文规定的规范化/离线范围关闭。README已分开当前普通连续能力与2026-09-03一轮canary历史。按约定单独本地提交，不推送、部署、改EA/profile、访问真实账户或发单；W4跨重启、A07、W6–W9仍未验收。下一步先执行既定Q3原生cache/订单/MT5票据归属探针，再按证据实施W6恢复与仍在线的stop/drain，不能从close canary外推重启已可用。

2026-09-06，Q3 重启归属技术前置完成；只接受原生接口/合成重放证据，不关闭W6父项或R01–R06：

- 从干净`475fb6d`开始，先在W6固定离线实验写集和判断标准。实施agent写真实MT5 adapter/native reports边界测试，主agent写独立进程codec/ExecutionEngine worker和方案；生产源码、配置/profile、schema、EA均未改。
- 11项native/adapter病例和3项新进程对照均通过。报告恢复能保留CID/identifier不等于保留原策略；完整原事件和显式索引能重建本组订单/仓位，orders-only无法重建Position，省略索引会丢未成交exact-close绑定。原生partial terminal/已闭单布尔成功的数量反例和普通BFX非零cold拒绝均固定，不把原生限制改成adapter豁免。
- 主agent在两个冻结测试文件上运行单次全量：**2306 passed / 129既有类别Pandas弃用警告 / 298.40s**。全仓Ruff、Mypy85文件、diff-check通过；新14项单文件5.45s通过，子集不累加进全量数量，测前测后文件SHA相同。
- 未实施reviewer独立14项通过，并做内存反事实：4个fill换UUID但保持TradeId不改变订单/仓位/事件；同一partial source换新TradeId后filled与Position由0.5增到1.0，排除无效事件/仅UUID去重的假绿色。其完整核对两个测试文件、W6/Q3新增方案和pinned原生cache源码后给出RECOMMEND-ACCEPT；主agent据上述独立证据及全量接受Q3。
- 后续按W6a原生cache → W6b事实/义务恢复 → W6c在线drain推进；框架Redis接线有源码依据但真实后端、队列收束及普通策略继续运行尚未认证。不搬测试worker做生产，不新增订单日志。本包按约定单独本地提交，不推送、启动服务、部署或交易；W4跨重启、A07、W6–W9保持未完成。

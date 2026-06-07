# Agent Notes — 累积知识索引

> 2026-06-07 从 Claude Code 的自动记忆迁移而来。每个 `.md` 是一条事实/经验。文中 `[[name]]`
> 指向同目录的兄弟文件 `name.md`。`metadata.type`:`user`(用户是谁)/ `project`(在做什么)/
> `feedback`(怎么做事的经验)/ `reference`(外部指针)。
> 注:个别文件含 Claude 特有 frontmatter(originSessionId 等)与"记忆 N 天前"提示——是迁移残留,
> 内容仍有效;**引用任何 file:line 前先对当前代码核实**(记忆是时点快照,可能过时)。

## 用户 / 协作
- [user_arbitrage_owner](user_arbitrage_owner.md) — 在真实个人账户上跑 polymarket/orbitexch 实盘套利;live test 会下真单。
- [feedback_verify_path_and_schema_source](feedback_verify_path_and_schema_source.md) — 两个易复发失误:① 别拿"测试通过"推断某代码路径已验(先确认它真跑了);② venue payload 的 schema 权威是老代码实读字段,不是精选 debug log。

## 项目进展
- [gap_c_oe_exec_live_validated](gap_c_oe_exec_live_validated.md) — **重要勘误**:`place_and_cancel` 跑老 services 栈、**不验** NT 适配器;Gap C connect/login/general WS 已经走 `launchers/arb_node.py` skip=true 验过,但 OE 下单/撤单/成交回执仍未真单验证。含实测 CURRENT_BETS WS 帧 schema(`offerId == venue_order_id` 是 join key)。
- [oe_competition_page_timeout_smoke68](oe_competition_page_timeout_smoke68.md) — Claude 最新会话迁移:#68 每 competition 一页后,competition 页保留 `networkidle`;OE 页面默认 timeout 统一为 120s;PM proxy 透传修复后,PM+OE 双边 OBD 同场到齐并触发 StrategyEvaluator 重评已用 NT-node skip=true live 验证。
- [project_dual_venv_layout](project_dual_venv_layout.md) — `.venv/`(uv,正统)vs `venv/`(旧、不全);切分支后"缺包"怪象多源于此。

## 开放 Bug
- [bug_polymarket_order_version_mismatch](bug_polymarket_order_version_mismatch.md) — PM POST /order 曾报 order_version_mismatch;2026-06-06 老栈跑未复发,疑已自愈,留观察。
- [bug_compensating_cancel_missing](bug_compensating_cancel_missing.md) — 单腿失败会把另一腿活跃挂单留在 venue,目前只能手动撤;补偿撤单触发逻辑未接。
- [bug_pm_exec_connect_balance_fatal](bug_pm_exec_connect_balance_fatal.md) — PM 余额检查是硬连接前置;瞬时抖动会挂起 node、trader 起不来(actors 卡 READY)。masking TypeError 已于 2026-06-04 修。

## 外部指针
- [reference_live_test_skill](reference_live_test_skill.md) — live-test skill(迁移后在 `~/.codex/skills/live-test/`);触发词"实盘测试 / live test / 跑一遍 X"。

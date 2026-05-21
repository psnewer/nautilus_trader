"""
异构 instrument 归一(Q9)的端到端验证。

PM 用 BinaryOption + OE 用 BettingInstrument,通过 instrument.info dict 中的
6 个统一 key(sport / competition / home_team / away_team / start_ts / selection_role)
做匹配。本测试验证从 Provider 加载到 MatchedPair 输出的完整链路。

对应章节: refactor.md §6.4
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 3")
def test_e2e_heterogeneous_match():
    """
    matching-3.3 (e2e): PM BinaryOption 与 OE BettingInstrument 在 MatchingActor 中匹配成功

    前置:
    - PM Provider 加载若干 BinaryOption(每个 instrument.info 含 6 key)
    - OE Provider 加载若干 BettingInstrument(每个 instrument.info 含 6 key)
    - 测试数据中至少有一对 PM/OE instrument 共享相同 home_team + away_team + start_ts
    步骤:
    - 触发 MatchingActor _do_match
    期望:
    - MatchedPair 输出含上述配对
    - MatchEngine 在执行过程中没有 isinstance 检查具体类型
    """

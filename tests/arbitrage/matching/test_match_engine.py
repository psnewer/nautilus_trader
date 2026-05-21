"""
MatchEngine 测试(摘要)。详细用例待 Step 3 启动时展开。

MatchEngine 是 src/arbitrage/matching/engine.py 的核心算法,
代码从 services/market_matching/engine.py 平移,但输入数据形态改变:
- 之前: 自定义 PM/OE event/market dict
- 之后: NT instrument 列表(BinaryOption + BettingInstrument 异构)

对应章节: refactor.md §5.3, §6.4
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 3")
def test_match_engine_cross_venue_pairing():
    """
    matching-2.x: MatchEngine 跨 venue 配对

    输入: PM BinaryOption 列表 + OE BettingInstrument 列表
    期望: 输出 MatchedPair 列表,每对的 info dict 6 个 key 对齐
    """


@pytest.mark.skip(reason="not implemented")
def test_match_engine_no_isinstance_dependency():
    """
    matching-3.3: 引擎不依赖具体 instrument 类型 (Q9)

    通过代码扫描验证 MatchEngine / EventNormalizer 中无:
    - isinstance(inst, BinaryOption)
    - isinstance(inst, BettingInstrument)
    只读 instrument.info 与 instrument.id。
    """

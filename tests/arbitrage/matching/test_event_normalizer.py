"""
EventNormalizer 测试(摘要)。详细用例待 Step 3 启动时展开。

EventNormalizer 是 src/arbitrage/matching/normalizer.py 的核心算法,
**代码从 services/market_matching/normalizer.py 原样平移**(P2: 领域 IP 保留)。

对应章节: refactor.md §5.3
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 3 execution")
def test_normalizer_single_venue_normalization():
    """
    matching-1.x: EventNormalizer 单 venue 归一化

    平移自原 services/market_matching/ 中的现有用例(如有);
    Step 3 启动时把现有测试搬过来即可,算法不变。
    """

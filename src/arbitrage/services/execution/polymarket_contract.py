"""
Polymarket 链上操作封装

通过 Builder Relayer 执行 CTF 合约的 mergePositions 和 redeemPositions。
支持标准二元市场 (negRisk=false) 和负风险多结果市场 (negRisk=true)。

合约地址:
- CTF: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
- NegRiskAdapter: 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
- USDC.e (collateral): 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

参考:
- https://docs.polymarket.com/developers/builders/relayer-client
- https://github.com/Polymarket/py-builder-relayer-client
"""

import logging
from dataclasses import dataclass
from typing import Any

from src.arbitrage.services.odds_subscription.config import OddsSubscriptionConfig

# 尝试导入 relayer 客户端
try:
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import OperationType, SafeTransaction
    from py_builder_relayer_client.builder import derive as _derive_mod
    from py_builder_relayer_client.builder import safe as _safe_mod
    from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
    HAS_RELAYER_CLIENT = True
except ImportError:
    HAS_RELAYER_CLIENT = False
    RelayClient = None
    _derive_mod = None
    _safe_mod = None

# 尝试导入 eth 工具
try:
    from eth_utils import keccak
    from eth_abi import encode
    HAS_ETH_UTILS = True
except ImportError:
    HAS_ETH_UTILS = False

# 合约地址 (Polygon)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polygon chain ID
POLYGON_CHAIN_ID = 137

# 零 bytes32
ZERO_BYTES32 = b"\x00" * 32


@dataclass
class TxResult:
    """链上交易结果"""
    success: bool
    tx_hash: str = ""
    message: str = ""
    raw_response: Any = None


def _function_selector(signature: str) -> bytes:
    """计算 Solidity 函数选择器 (前4字节)"""
    return keccak(text=signature)[:4]


def _encode_merge_positions_ctf(
    condition_id: str,
    amount: int,
) -> bytes:
    """
    编码 CTF.mergePositions calldata (negRisk=false)

    mergePositions(address collateral, bytes32 parentCollectionId, bytes32 conditionId, uint256[] partition, uint256 amount)
    partition = [1, 2] 表示两个 outcome slots
    """
    selector = _function_selector(
        "mergePositions(address,bytes32,bytes32,uint256[],uint256)"
    )
    # parentCollectionId = 0x0 (根集合)
    # partition = [1, 2] (二元市场的两个 outcome)
    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [
            USDC_E_ADDRESS,
            ZERO_BYTES32,
            bytes.fromhex(condition_id.replace("0x", "")),
            [1, 2],
            amount,
        ],
    )
    return selector + encoded


def _encode_merge_positions_neg_risk(
    condition_id: str,
    amount: int,
) -> bytes:
    """
    编码 NegRiskAdapter.mergePositions calldata (negRisk=true)

    mergePositions(bytes32 conditionId, uint256 amount)
    """
    selector = _function_selector("mergePositions(bytes32,uint256)")
    encoded = encode(
        ["bytes32", "uint256"],
        [
            bytes.fromhex(condition_id.replace("0x", "")),
            amount,
        ],
    )
    return selector + encoded


def _encode_redeem_positions_ctf(condition_id: str) -> bytes:
    """
    编码 CTF.redeemPositions calldata (negRisk=false)

    redeemPositions(address collateral, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)
    indexSets = [1, 2] 赎回所有 outcome slots
    """
    selector = _function_selector(
        "redeemPositions(address,bytes32,bytes32,uint256[])"
    )
    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            USDC_E_ADDRESS,
            ZERO_BYTES32,
            bytes.fromhex(condition_id.replace("0x", "")),
            [1, 2],
        ],
    )
    return selector + encoded


def _encode_redeem_positions_neg_risk(
    condition_id: str,
    amounts: list[int],
) -> bytes:
    """
    编码 NegRiskAdapter.redeemPositions calldata (negRisk=true)

    redeemPositions(bytes32 conditionId, uint256[] amounts)
    """
    selector = _function_selector("redeemPositions(bytes32,uint256[])")
    encoded = encode(
        ["bytes32", "uint256[]"],
        [
            bytes.fromhex(condition_id.replace("0x", "")),
            amounts,
        ],
    )
    return selector + encoded


class PolymarketContractService:
    """
    通过 Builder Relayer 执行 Polymarket 链上操作

    主要功能:
    - mergePositions: 合并同一 condition 下两个 outcome token 回 USDC.e
    - redeemPositions: 赎回已结算市场的 winning tokens
    """

    def __init__(
        self,
        config: OddsSubscriptionConfig,
        logger: logging.Logger | None = None,
    ):
        self._config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._client: RelayClient | None = None
        self._initialized = False

    async def initialize(self) -> bool:
        """
        初始化 Relayer 客户端

        Returns:
            是否初始化成功
        """
        if not HAS_RELAYER_CLIENT:
            self._log.warning(
                "py-builder-relayer-client not installed. "
                "Install with: pip install py-builder-relayer-client"
            )
            return False

        if not HAS_ETH_UTILS:
            self._log.warning(
                "eth_utils/eth_abi not installed. "
                "Install with: pip install eth-utils eth-abi"
            )
            return False

        if not self._config.polymarket_private_key:
            self._log.warning("Polymarket private key not configured")
            return False

        if not self._config.polymarket_funder:
            self._log.warning("Polymarket funder (proxy wallet) not configured")
            return False

        try:
            builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=self._config.polymarket_builder_api_key,
                    secret=self._config.polymarket_builder_api_secret,
                    passphrase=self._config.polymarket_builder_passphrase,
                )
            )

            self._client = RelayClient(
                self._config.polymarket_relayer_url,
                POLYGON_CHAIN_ID,
                self._config.polymarket_private_key,
                builder_config,
            )

            # 验证 proxy wallet 地址
            proxy_wallet = self._config.polymarket_funder
            derived_safe = self._client.get_expected_safe()
            if derived_safe.lower() != proxy_wallet.lower():
                self._log.warning(
                    f"SDK derived Safe ({derived_safe}) != configured proxy wallet ({proxy_wallet}). "
                    f"Will override derive() to use proxy wallet."
                )

            self._initialized = True
            self._log.info(
                f"PolymarketContractService initialized, proxy_wallet={proxy_wallet}"
            )
            return True

        except Exception as e:
            self._log.error(f"Failed to initialize relayer client: {e}")
            return False

    def _execute_with_proxy(
        self,
        transactions: list,
        metadata: str = "",
    ):
        """
        使用实际 proxy wallet 地址执行 Relayer 交易

        SDK 内部通过 CREATE2 派生 Safe 地址，可能与实际 proxy wallet 不匹配。
        通过临时替换 derive 函数，强制使用配置中的 proxy wallet 地址。

        需要 patch 两处:
        - _derive_mod.derive: 被 client.py 的 get_expected_safe() 调用
        - _safe_mod.derive: 被 safe.py 的 build_safe_transaction_request() 调用
          (safe.py 用 from .derive import derive 创建了本地引用)
        """
        proxy_wallet = self._config.polymarket_funder
        override = lambda addr, factory: proxy_wallet

        # 保存原始引用
        orig_derive_mod = _derive_mod.derive
        orig_safe_mod = _safe_mod.derive

        self._log.info(f"Executing with proxy wallet override: {proxy_wallet}")

        # Patch both locations
        _derive_mod.derive = override
        _safe_mod.derive = override
        try:
            return self._client.execute(transactions, metadata)
        finally:
            _derive_mod.derive = orig_derive_mod
            _safe_mod.derive = orig_safe_mod

    async def merge_positions(
        self,
        condition_id: str,
        amount: float,
        neg_risk: bool = False,
    ) -> TxResult:
        """
        合并仓位回 USDC.e

        同一 condition 下持有两个 outcome token 时，可以 merge 回 collateral。
        merge_amount = min(token_0_size, token_1_size)

        Args:
            condition_id: 市场 condition ID
            amount: 合并数量 (USDC 精度, 6 decimals)
            neg_risk: 是否为负风险市场

        Returns:
            交易结果
        """
        if not self._initialized or not self._client:
            return TxResult(success=False, message="Service not initialized")

        # 转换为 wei 精度 (USDC 6 decimals)
        amount_raw = int(amount * 1_000_000)
        if amount_raw <= 0:
            return TxResult(success=False, message=f"Invalid amount: {amount}")

        try:
            if neg_risk:
                calldata = _encode_merge_positions_neg_risk(condition_id, amount_raw)
                target = NEG_RISK_ADAPTER_ADDRESS
                desc = f"merge negRisk positions: condition={condition_id[:16]}..., amount={amount}"
            else:
                calldata = _encode_merge_positions_ctf(condition_id, amount_raw)
                target = CTF_ADDRESS
                desc = f"merge CTF positions: condition={condition_id[:16]}..., amount={amount}"

            self._log.info(f"Executing {desc}")

            txn = SafeTransaction(
                to=target,
                value="0",
                data="0x" + calldata.hex(),
                operation=OperationType.Call,
            )

            resp = self._execute_with_proxy([txn], desc)
            self._log.info(f"Merge submitted: {resp}")

            # 等待确认
            awaited = resp.wait()
            self._log.info(f"Merge confirmed: {awaited}")

            return TxResult(
                success=True,
                tx_hash=getattr(awaited, "transactionHash", ""),
                message=desc,
                raw_response=awaited,
            )

        except Exception as e:
            self._log.error(f"Merge failed: {e}")
            return TxResult(success=False, message=str(e))

    async def redeem_positions(
        self,
        condition_id: str,
        neg_risk: bool = False,
        amounts: list[float] | None = None,
    ) -> TxResult:
        """
        赎回已结算市场的 winning tokens

        Args:
            condition_id: 市场 condition ID
            neg_risk: 是否为负风险市场
            amounts: (仅 negRisk) 各 outcome slot 的赎回量

        Returns:
            交易结果
        """
        if not self._initialized or not self._client:
            return TxResult(success=False, message="Service not initialized")

        try:
            if neg_risk:
                if not amounts:
                    return TxResult(
                        success=False,
                        message="amounts required for negRisk redeem",
                    )
                amounts_raw = [int(a * 1_000_000) for a in amounts]
                calldata = _encode_redeem_positions_neg_risk(condition_id, amounts_raw)
                target = NEG_RISK_ADAPTER_ADDRESS
                desc = f"redeem negRisk: condition={condition_id[:16]}..."
            else:
                calldata = _encode_redeem_positions_ctf(condition_id)
                target = CTF_ADDRESS
                desc = f"redeem CTF: condition={condition_id[:16]}..."

            self._log.info(f"Executing {desc}")

            txn = SafeTransaction(
                to=target,
                value="0",
                data="0x" + calldata.hex(),
                operation=OperationType.Call,
            )

            resp = self._execute_with_proxy([txn], desc)
            self._log.info(f"Redeem submitted: {resp}")

            awaited = resp.wait()
            self._log.info(f"Redeem confirmed: {awaited}")

            return TxResult(
                success=True,
                tx_hash=getattr(awaited, "transactionHash", ""),
                message=desc,
                raw_response=awaited,
            )

        except Exception as e:
            self._log.error(f"Redeem failed: {e}")
            return TxResult(success=False, message=str(e))

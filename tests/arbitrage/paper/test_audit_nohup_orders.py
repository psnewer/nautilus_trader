import json
import subprocess
import sys
from pathlib import Path


def test_audit_uses_game_id_for_doubles_and_marks_pre_fill(tmp_path):
    log_path = tmp_path / "nohup.out"
    output_path = tmp_path / "audit.json"
    log_path.write_text(
        "\n".join([
            "2026-10-15T08:00:00.000000000Z [INFO] MatchedPair Tennis|PreA|PreB (conf=2.00)",
            "2026-10-15T08:00:00.010000000Z [INFO] SubscribeData(params={'game_id': 101})",
            "2026-10-15T08:00:01.000000000Z [INFO] Sports phase updated: game=101 phase=PRE source=test",
            "2026-10-15T08:01:00.000000000Z [INFO] OrderInitialized(instrument_id=x.POLYMARKET, client_order_id=ARB-pre, side=BUY, type=LIMIT, quantity=5.050000, options={'price': '0.20'}, tags=['arb:pair_id=Tennis|PreA|PreB', 'arb:leg_key=polymarket:no:0'])",
            "2026-10-15T08:01:01.000000000Z [INFO] OrderFilled(client_order_id=ARB-pre, last_qty=5.050000, last_px=0.20)",
            "2026-10-15T09:00:00.000000000Z [INFO] MatchedPair Tennis|DoublesAPlayer2|DoublesBPlayer2 (conf=2.00)",
            "2026-10-15T09:00:00.010000000Z [INFO] SubscribeData(params={'game_id': 202})",
            "2026-10-15T09:00:01.000000000Z [INFO] Sports phase updated: game=202 phase=IN_PLAY source=test",
            "2026-10-15T09:00:02.000000000Z [INFO] Sports score updated: game=202 Doubles A vs Doubles B score=<none>->0-0 period=S1 elapsed= status=inprogress",
            "2026-10-15T09:01:00.000000000Z [INFO] OrderInitialized(instrument_id=x.POLYMARKET, client_order_id=ARB-live, side=BUY, type=LIMIT, quantity=5.050000, options={'price': '0.30'}, tags=['arb:pair_id=Tennis|DoublesAPlayer2|DoublesBPlayer2', 'arb:leg_key=polymarket:yes:0'])",
            "2026-10-15T09:01:01.000000000Z [INFO] OrderFilled(client_order_id=ARB-live, last_qty=5.050000, last_px=0.30)",
        ]),
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(Path("scripts/arbitrage_audit/audit_nohup_orders.py")),
            str(log_path),
            str(output_path),
        ],
        check=True,
    )

    rows = {
        row["client_order_id"]: row
        for row in json.loads(output_path.read_text(encoding="utf-8"))["rows"]
    }
    assert rows["ARB-pre"]["首次成交比分"] == "PRE"
    assert rows["ARB-pre"]["首次成交盘/局"] == "PRE"
    assert rows["ARB-live"]["首次成交比分"] == "0-0"
    assert rows["ARB-live"]["首次成交盘/局"] == "S1"


def test_audit_preserves_apostrophe_in_pair_id_and_links_score(tmp_path):
    log_path = tmp_path / "nohup.out"
    output_path = tmp_path / "audit.json"
    log_path.write_text(
        "\n".join([
            "2026-11-22T01:00:00.000000000Z [INFO] MatchedPair Tennis|Zane Stevens|Benjamin O'Connell (conf=2.00)",
            "2026-11-22T01:00:00.010000000Z [INFO] SubscribeData(params={'game_id': 303})",
            "2026-11-22T01:00:01.000000000Z [INFO] Sports score updated: game=303  vs  score=<none>->2-2 period=S1 elapsed= status=",
            '2026-11-22T01:01:00.000000000Z [INFO] OrderInitialized(instrument_id=x.POLYMARKET, client_order_id=ARB-apostrophe, side=BUY, type=LIMIT, quantity=5.050000, options={\'price\': \'0.57\'}, tags=["arb:pair_id=Tennis|Zane Stevens|Benjamin O\'Connell", \'arb:leg_key=polymarket:yes:0\'])',
            "2026-11-22T01:01:01.000000000Z [INFO] OrderFilled(client_order_id=ARB-apostrophe, last_qty=5.050000, last_px=0.57)",
        ]),
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(Path("scripts/arbitrage_audit/audit_nohup_orders.py")),
            str(log_path),
            str(output_path),
        ],
        check=True,
    )

    row = json.loads(output_path.read_text(encoding="utf-8"))["rows"][0]
    assert row["选手2"] == "Benjamin O'Connell"
    assert row["下单比分"] == "2-2"
    assert row["首次成交比分"] == "2-2"


def test_audit_links_inferred_fill_to_latest_live_strategy_order(tmp_path):
    log_path = tmp_path / "nohup.out"
    output_path = tmp_path / "audit.json"
    instrument = "condition-token.POLYMARKET"
    log_path.write_text(
        "\n".join([
            "2026-11-27T12:00:00.000000000Z [INFO] MatchedPair Tennis|Federico Bondioli|Semen Pankin (conf=2.00)",
            "2026-11-27T12:00:00.010000000Z [INFO] SubscribeData(params={'game_id': 404})",
            "2026-11-27T12:00:01.000000000Z [INFO] Sports score updated: game=404 Federico Bondioli vs Semen Pankin score=<none>->2-2 period=S1 elapsed= status=inprogress",
            f"2026-11-27T12:01:00.000000000Z [INFO] OrderInitialized(instrument_id={instrument}, client_order_id=ARB-old, side=BUY, type=LIMIT, quantity=5.050000, options={{'price': '0.59'}}, tags=['arb:pair_id=Tennis|Federico Bondioli|Semen Pankin', 'arb:leg_key=polymarket:yes:0'])",
            "2026-11-27T12:02:00.000000000Z [INFO] <--[EVT] OrderCanceled(client_order_id=ARB-old, account_id=POLYMARKET-001)",
            f"2026-11-27T12:03:00.000000000Z [INFO] OrderInitialized(instrument_id={instrument}, client_order_id=ARB-live, side=BUY, type=LIMIT, quantity=5.050000, options={{'price': '0.59'}}, tags=['arb:pair_id=Tennis|Federico Bondioli|Semen Pankin', 'arb:leg_key=polymarket:yes:0'])",
            f"2026-11-27T12:04:00.000000000Z [INFO] Generated inferred OrderFilled(instrument_id={instrument}, client_order_id=external, order_side=BUY, last_qty=5.050000, last_px=0.59 USDC.e)",
            "2026-11-27T12:05:00.000000000Z [INFO] <--[EVT] OrderRejected(client_order_id=ARB-live, account_id=POLYMARKET-001)",
        ]),
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(Path("scripts/arbitrage_audit/audit_nohup_orders.py")),
            str(log_path),
            str(output_path),
        ],
        check=True,
    )

    rows = {
        row["client_order_id"]: row
        for row in json.loads(output_path.read_text(encoding="utf-8"))["rows"]
    }
    assert rows["ARB-old"]["状态"] == "未成交-已撤单"
    assert rows["ARB-live"]["状态"] == "已成交"
    assert rows["ARB-live"]["已成交量"] == 5.05
    assert rows["ARB-live"]["成交均价"] == 0.59

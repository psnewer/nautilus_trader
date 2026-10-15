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

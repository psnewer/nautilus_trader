"""SharpExch price frame parser 测试。"""

from nautilus_trader.adapters.sharpexch.message_parser import SharpExchMessageParser


def test_parse_price_message_parses_biab_dict_levels():
    parser = SharpExchMessageParser()
    parsed = parser.parse_price_message(
        {
            "id": "1.259502313",
            "mainEventId": "5843495",
            "mainEventName": "Rafael Jodar v Felix Gill",
            "marketNameWithParents": "Match Odds",
            "rc": [
                {
                    "id": 111,
                    "bdatb": [{"index": 0, "odds": 2.0, "amount": 10}],
                    "bdatl": [{"index": 0, "odds": 2.1, "amount": 5}],
                    "tv": 100,
                    "locked": False,
                },
            ],
            "marketDefinition": {"status": "OPEN", "inPlay": False},
            "apiPt": 1782768600000,
        },
    )
    assert parsed is not None
    assert parsed["market_id"] == "1.259502313"
    assert parsed["event_id"] == "5843495"
    assert parsed["status"] == "OPEN"
    assert parsed["in_play"] is False
    assert parsed["timestamp"] == 1782768600000
    runner = parsed["runners"][0]
    assert runner["selection_id"] == "111"
    assert runner["back"] == [{"price": 2.0, "size": 10.0}]
    assert runner["lay"] == [{"price": 2.1, "size": 5.0}]


def test_parse_price_message_supports_list_levels():
    parser = SharpExchMessageParser()
    parsed = parser.parse_price_message(
        {
            "id": "1.259502313",
            "rc": [{"id": 111, "batb": [[0, 2.0, 10]], "batl": [[0, 2.1, 5]]}],
            "marketDefinition": {},
        },
    )
    runner = parsed["runners"][0]
    assert runner["back"] == [{"price": 2.0, "size": 10.0}]
    assert runner["lay"] == [{"price": 2.1, "size": 5.0}]


def test_parse_price_message_missing_market_id_returns_none():
    parser = SharpExchMessageParser()
    assert parser.parse_price_message({"rc": []}) is None


def test_get_runner_by_selection_id():
    parser = SharpExchMessageParser()
    parsed = {"runners": [{"selection_id": "111"}, {"selection_id": "222"}]}
    assert parser.get_runner_by_selection_id(parsed, "222") == {"selection_id": "222"}
    assert parser.get_runner_by_selection_id(parsed, "333") is None


def test_parse_general_frame_balance_dict_payload():
    parser = SharpExchMessageParser()
    parsed = parser.parse_general_frame({"BALANCE": {"balance": "37.49", "avBalance": None}})
    assert parsed == {"type": "balance", "balance": 37.49, "av_balance": None}


def test_parse_general_frame_balance_nested_json_payload():
    parser = SharpExchMessageParser()
    parsed = parser.parse_general_frame({"BALANCE": '{"balance":"12.34","avBalance":"10.00"}'})
    assert parsed == {"type": "balance", "balance": 12.34, "av_balance": 10.0}


def test_parse_general_frame_current_bets_filters_non_dict_items():
    parser = SharpExchMessageParser()
    parsed = parser.parse_general_frame(
        {
            "CURRENT_BETS": [
                {"marketId": "1.259502313", "selectionId": 111},
                "ignored",
            ],
        },
    )
    assert parsed == {
        "type": "current_bets",
        "bets": [{"marketId": "1.259502313", "selectionId": 111}],
    }


def test_parse_general_frame_current_bets_nested_json_payload():
    parser = SharpExchMessageParser()
    parsed = parser.parse_general_frame({"CURRENT_BETS": '[{"marketId":"1.259502313"}]'})
    assert parsed == {"type": "current_bets", "bets": [{"marketId": "1.259502313"}]}


def test_parse_general_frame_unknown_returns_none():
    parser = SharpExchMessageParser()
    assert parser.parse_general_frame({"OTHER": {}}) is None
    assert parser.parse_general_frame("not a dict") is None


def test_parse_order_message_aliases_general_frame():
    parser = SharpExchMessageParser()
    assert parser.parse_order_message({"BALANCE": {"balance": "1"}}) == {
        "type": "balance",
        "balance": 1.0,
        "av_balance": None,
    }

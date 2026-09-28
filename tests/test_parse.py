from pm_scanner.kalshi import parse_kalshi_market, parse_kalshi_orderbook
from pm_scanner.polymarket import parse_book, parse_event, parse_market


def test_gamma_market_parses_json_encoded_lists():
    m = parse_market(
        {
            "id": "1",
            "question": "Q?",
            "slug": "q",
            "conditionId": "0x1",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["111", "222"]',
            "outcomePrices": '["0.7", "0.3"]',
            "bestBid": "0.69",
            "bestAsk": 0.71,
            "endDate": "2026-10-01T00:00:00Z",
        }
    )
    assert m.is_binary and m.yes_token == "111" and m.no_token == "222"
    assert m.outcome_prices == [0.7, 0.3]
    assert m.best_bid == 0.69 and m.no_best_ask == 0.31 and m.no_best_bid == 0.29
    assert m.end_date.tzinfo is not None


def test_gamma_event_tags_and_negrisk_inherit():
    ev = parse_event({"id": "e", "title": "T", "slug": "t", "tags": [{"slug": "Politics"}, "Crypto"], "markets": [{"negRisk": True, "clobTokenIds": "[]"}]})
    assert ev.tags == ["politics", "crypto"]
    assert ev.neg_risk is True


def test_clob_book_best_levels():
    b = parse_book({"asset_id": "t", "bids": [{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "5"}], "asks": [{"price": "0.45", "size": "7"}, {"price": "0.44", "size": "0"}]})
    assert b.best_bid.price == 0.42 and b.best_ask.price == 0.45  # zero-size level dropped


def test_kalshi_market_accepts_cents_or_dollars():
    cents = parse_kalshi_market({"ticker": "A", "yes_bid": 40, "yes_ask": 42, "no_bid": 58, "no_ask": 60})
    dollars = parse_kalshi_market({"ticker": "B", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.42", "no_bid_dollars": "0.58", "no_ask_dollars": "0.60"})
    assert (cents.yes_bid, cents.no_ask) == (0.40, 0.60)
    assert (dollars.yes_ask, dollars.no_bid) == (0.42, 0.58)


def test_kalshi_orderbook_mirrors_bids_into_asks():
    yes_book, no_book = parse_kalshi_orderbook("A", {"orderbook": {"yes": [[40, 100]], "no": [[58, 150]]}})
    assert yes_book.best_bid.price == 0.40
    assert yes_book.best_ask.price == 0.42 and yes_book.best_ask.size == 150
    assert no_book.best_ask.price == 0.60 and no_book.best_ask.size == 100

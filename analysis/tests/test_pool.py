"""The dashboard's pool maths must equal the contract's. The vectors below are also asserted in
test/PoolVectors.t.sol against the real contract."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pool as P  # noqa: E402

W = P.WAD


def test_vectors_match_the_contract_70pct_2pct_fee():
    p = P.Pool.seed(1000 * W, 7000, 200)
    assert (p.y, p.n) == (428571428571428571428, 1000000000000000000000)
    assert p.price_yes_wad() == 700000000000000000
    assert p.quote_buy(True, 100 * W) == (136251366120218579234, 2000000000000000000)
    assert p.quote_buy(False, 375 * W // 10) == (115727665208381303246, 750000000000000000)
    # fees that aren't whole numbers: rounded UP, as in the contract
    assert p.quote_buy(True, 7 * W + 3) == (9779969012573744118, 140000000000000001)
    assert p.quote_buy(False, 123456789) == (403292176, 2469136)
    q = p.after_buy(True, 100 * W)
    assert (q.y, q.n) == (392320062451209992194, 1100000000000000000000)
    assert q.price_yes_wad() == 737107291979439880
    assert q.quote_sell(True, 50 * W) == (70102123263032329090, 1020408163265306123)


def test_vectors_match_the_contract_25pct_no_fee():
    p = P.Pool.seed(500 * W, 2500, 0)
    assert (p.y, p.n) == (500000000000000000000, 166666666666666666666)
    assert p.price_yes_wad() == 249999999999999999
    assert p.quote_buy(True, 7 * W + 3) == (27153550863723608456, 0)


def test_buying_raises_the_price_and_never_shrinks_k():
    p = P.Pool.seed(1000 * W, 5000, 200)
    for side in (True, False):
        a = p.after_buy(side, 50 * W)
        assert (a.price_yes() > p.price_yes()) == side
        assert a.y * a.n >= p.y * p.n
    s = p.after_buy(True, 50 * W).after_sell(True, 20 * W)
    assert s.y * s.n >= p.y * p.n * 0.999


def test_bad_inputs_are_refused():
    with pytest.raises(P.PoolError):
        P.Pool.seed(1000 * W, 50, 0)
    with pytest.raises(P.PoolError):
        P.Pool.seed(1000 * W, 5000, 1001)
    with pytest.raises(P.PoolError):
        P.Pool.seed(1000 * W, 5000, 0).quote_sell(True, 1000 * W)

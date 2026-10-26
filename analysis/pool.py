"""Python mirror of src/FixedProductMarketMaker.sol (M2), for the dashboard's trade calculator.

Same integer arithmetic and the same rounding as the contract (amounts in wei, 1e18 = 1 token),
so a quote here equals the contract's quote. test/PoolVectors.t.sol and tests/test_pool.py check
the same hard-coded numbers against the contract and against this file.
"""

from __future__ import annotations

from dataclasses import dataclass

BPS = 10_000
MAX_FEE_BPS = 1_000
MIN_PROB_BPS = 100
MINIMUM_LIQUIDITY = 1000
WAD = 10**18


def mul_div(a: int, b: int, d: int, ceil: bool = False) -> int:
    """OpenZeppelin Math.mulDiv: floor(a * b / d), or rounded up if ceil."""
    q, r = divmod(a * b, d)
    return q + 1 if ceil and r else q


class PoolError(ValueError):
    pass


@dataclass(frozen=True)
class Pool:
    y: int  # YES reserve
    n: int  # NO reserve
    fee_bps: int

    @staticmethod
    def seed(amount: int, yes_prob_bps: int, fee_bps: int) -> "Pool":
        """The first addLiquidity: `amount` collateral at opening odds yes_prob_bps (0 = 50%)."""
        if fee_bps > MAX_FEE_BPS:
            raise PoolError("fee above 10%")
        p = BPS // 2 if yes_prob_bps == 0 else yes_prob_bps
        if p < MIN_PROB_BPS or p > BPS - MIN_PROB_BPS:
            raise PoolError("opening odds must be between 1% and 99%")
        if amount <= MINIMUM_LIQUIDITY:
            raise PoolError("amount too small")
        if p >= BPS // 2:
            keep_n, keep_y = amount, mul_div(amount, BPS - p, p)
        else:
            keep_y, keep_n = amount, mul_div(amount, p, BPS - p)
        return Pool(keep_y, keep_n, fee_bps)

    def price_yes(self) -> float:
        return self.n / (self.y + self.n)

    def price_yes_wad(self) -> int:
        return mul_div(self.n, WAD, self.y + self.n)

    def quote_buy(self, is_yes: bool, collateral_in: int) -> tuple[int, int]:
        """(tokens_out, fee) for spending collateral_in."""
        if self.y == 0 or self.n == 0:
            raise PoolError("no liquidity")
        buy_r, other_r = (self.y, self.n) if is_yes else (self.n, self.y)
        fee = mul_div(collateral_in, self.fee_bps, BPS, ceil=True)
        invest = collateral_in - fee
        new_buy_r = mul_div(buy_r, other_r, other_r + invest, ceil=True)
        return buy_r + invest - new_buy_r, fee

    def quote_sell(self, is_yes: bool, collateral_out: int) -> tuple[int, int]:
        """(tokens_in, fee) to receive collateral_out."""
        if self.y == 0 or self.n == 0:
            raise PoolError("no liquidity")
        sell_r, other_r = (self.y, self.n) if is_yes else (self.n, self.y)
        gross = mul_div(collateral_out, BPS, BPS - self.fee_bps, ceil=True)
        fee = gross - collateral_out
        if gross >= other_r:
            raise PoolError("not enough liquidity for that sale")
        new_sell_r = mul_div(sell_r, other_r, other_r - gross, ceil=True)
        return new_sell_r + gross - sell_r, fee

    def after_buy(self, is_yes: bool, collateral_in: int) -> "Pool":
        out, _ = self.quote_buy(is_yes, collateral_in)
        if is_yes:
            return Pool(self.y + collateral_in - out, self.n + collateral_in, self.fee_bps)
        return Pool(self.y + collateral_in, self.n + collateral_in - out, self.fee_bps)

    def after_sell(self, is_yes: bool, collateral_out: int) -> "Pool":
        tokens_in, _ = self.quote_sell(is_yes, collateral_out)
        if is_yes:
            return Pool(self.y + tokens_in - collateral_out, self.n - collateral_out, self.fee_bps)
        return Pool(self.y - collateral_out, self.n + tokens_in - collateral_out, self.fee_bps)

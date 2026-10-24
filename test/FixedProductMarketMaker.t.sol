// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BinaryMarket} from "../src/BinaryMarket.sol";
import {FixedProductMarketMaker as FPMM} from "../src/FixedProductMarketMaker.sol";
import {MockERC20, FeeToken} from "./utils/MockERC20.sol";

contract FixedProductMarketMakerTest is Test {
    BinaryMarket internal m;
    FPMM internal pool;
    MockERC20 internal usd;
    IERC20 internal yesT;
    IERC20 internal noT;

    address internal resolver = makeAddr("resolver");
    address internal arbiter = makeAddr("arbiter");
    address internal lp = makeAddr("lp");
    address internal lp2 = makeAddr("lp2");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    uint64 internal closeAt;
    uint256 internal constant FEE = 200; // 2%

    function setUp() public {
        usd = new MockERC20();
        closeAt = uint64(block.timestamp + 7 days);
        m = new BinaryMarket(IERC20(address(usd)), "q", closeAt, 2 days, 100e18, resolver, arbiter);
        pool = new FPMM(m, FEE);
        yesT = IERC20(address(m.yes()));
        noT = IERC20(address(m.no()));
        address[4] memory users = [lp, lp2, alice, bob];
        for (uint256 i = 0; i < 4; i++) {
            usd.mint(users[i], 1_000_000e18);
            vm.startPrank(users[i]);
            usd.approve(address(pool), type(uint256).max);
            usd.approve(address(m), type(uint256).max);
            yesT.approve(address(pool), type(uint256).max);
            noT.approve(address(pool), type(uint256).max);
            vm.stopPrank();
        }
    }

    // ---- helpers ---------------------------------------------------------------------------

    function _seed(uint256 amount, uint256 probBps) internal returns (uint256 shares) {
        vm.prank(lp);
        shares = pool.addLiquidity(amount, probBps, 0);
    }

    function _k() internal view returns (uint256) {
        (uint256 y, uint256 n) = pool.reserves();
        return y * n;
    }

    function _settle(BinaryMarket.Outcome o) internal {
        vm.warp(closeAt);
        vm.prank(resolver);
        m.proposeOutcome(o);
        skip(2 days);
        m.finalize();
    }

    /// Largest collateralOut a holder of `tokens` can get by selling them (binary search on quoteSell).
    function _maxSell(bool isYes, uint256 tokens) internal view returns (uint256 best) {
        (uint256 y, uint256 n) = pool.reserves();
        uint256 hi = isYes ? n : y; // can't redeem more sets than the other side holds
        uint256 lo = 0;
        while (lo < hi) {
            uint256 mid = (lo + hi + 1) / 2;
            (bool ok, uint256 need) = _tryQuoteSell(isYes, mid);
            if (ok && need <= tokens) lo = mid;
            else hi = mid - 1;
        }
        return lo;
    }

    function _tryQuoteSell(bool isYes, uint256 out) internal view returns (bool, uint256) {
        if (out == 0) return (true, 0);
        try pool.quoteSell(isYes, out) returns (uint256 tokensIn, uint256) {
            return (true, tokensIn);
        } catch {
            return (false, 0);
        }
    }

    // ---- liquidity -------------------------------------------------------------------------

    function test_firstLiquidity_defaultsTo50Percent() public {
        uint256 shares = _seed(1000e18, 0);
        assertEq(shares, 1000e18 - 1000, "1000 wei of shares are locked forever");
        assertEq(pool.totalSupply(), 1000e18);
        (uint256 y, uint256 n) = pool.reserves();
        assertEq(y, 1000e18);
        assertEq(n, 1000e18);
        assertEq(pool.priceYes(), 0.5e18);
        assertEq(usd.balanceOf(address(pool)), 0, "all collateral becomes complete sets");
    }

    function test_firstLiquidity_setsOpeningOdds_andReturnsExtraTokens() public {
        _seed(1000e18, 7000); // 70% YES
        assertApproxEqAbs(pool.priceYes(), 0.7e18, 1e6);
        // the pool keeps all 1000 NO and 1000*0.3/0.7 YES; the rest of the YES goes back to the LP
        assertEq(noT.balanceOf(lp), 0);
        assertApproxEqAbs(yesT.balanceOf(lp), 1000e18 - uint256(1000e18) * 3 / 7, 1);
        _seed(0.1e18, 0); // later "probability" arguments are ignored
        assertApproxEqAbs(pool.priceYes(), 0.7e18, 1e6);
    }

    function test_openingOddsMustBeBetween1And99Percent() public {
        vm.startPrank(lp);
        vm.expectRevert(FPMM.BadProbability.selector);
        pool.addLiquidity(100e18, 99, 0);
        vm.expectRevert(FPMM.BadProbability.selector);
        pool.addLiquidity(100e18, 9901, 0);
        pool.addLiquidity(100e18, 9900, 0);
        vm.stopPrank();
    }

    function test_laterLiquidity_joinsAtCurrentOdds_withProportionalShares() public {
        _seed(1000e18, 0);
        vm.prank(alice);
        pool.buy(true, 300e18, 0); // move the price
        uint256 p = pool.priceYes();
        (uint256 y0, uint256 n0) = pool.reserves();
        uint256 s0 = pool.totalSupply();

        vm.prank(lp2);
        uint256 shares = pool.addLiquidity(500e18, 1234, 0);
        (uint256 y1, uint256 n1) = pool.reserves();
        assertApproxEqAbs(pool.priceYes(), p, 1e3, "joining doesn't move the price");
        // shares / supply == the fraction of each reserve contributed
        assertApproxEqRel(shares * 1e18 / (s0 + shares), (y1 - y0) * 1e18 / y1, 1e9);
        assertApproxEqRel(shares * 1e18 / (s0 + shares), (n1 - n0) * 1e18 / n1, 1e9);
        // the side the pool didn't need comes back to the provider
        assertGt(yesT.balanceOf(lp2) + noT.balanceOf(lp2), 0);
    }

    /// The first-depositor (inflation) attack: hold 1 share, donate to inflate it, and make the
    /// next provider's shares round down. The 1000 locked shares make it ruinous for the attacker.
    function test_inflationAttack_costsTheAttackerFarMoreThanTheVictim() public {
        uint256 attackerStart = usd.balanceOf(bob);
        vm.startPrank(bob);
        pool.addLiquidity(1001, 0, 0); // 1 share for bob, 1000 locked
        m.mintSets(10_000e18);
        yesT.transfer(address(pool), 10_000e18); // donate to inflate the share price
        noT.transfer(address(pool), 10_000e18);
        vm.stopPrank();

        vm.prank(lp2);
        uint256 victimShares = pool.addLiquidity(10_000e18, 0, 0);
        assertGt(victimShares, 0);

        (uint256 y, uint256 n) = pool.reserves();
        uint256 victimValue = Math.mulDiv(Math.min(y, n), victimShares, pool.totalSupply());
        uint256 attackerValue = Math.mulDiv(Math.min(y, n), 1, pool.totalSupply());
        uint256 victimLoss = 10_000e18 - victimValue;
        uint256 attackerLoss = attackerStart - usd.balanceOf(bob) - attackerValue;
        console.log("victim loss (bps of deposit):", victimLoss * 10_000 / 10_000e18);
        console.log("attacker loss per unit the victim loses:", attackerLoss / victimLoss);
        assertLt(victimLoss, 10_000e18 / 1000, "victim loses under 0.1%");
        assertGt(attackerLoss, victimLoss * 1000, "attacker loses over 1000x more");
    }

    function test_removeLiquidity_returnsShareOfBothSides() public {
        uint256 shares = _seed(1000e18, 0);
        vm.prank(alice);
        pool.buy(true, 100e18, 0);
        (uint256 y, uint256 n) = pool.reserves();
        vm.prank(lp);
        (uint256 yo, uint256 no_) = pool.removeLiquidity(shares / 4, 0, 0);
        assertEq(yo, Math.mulDiv(y, shares / 4, pool.totalSupply() + shares / 4));
        assertEq(no_, Math.mulDiv(n, shares / 4, pool.totalSupply() + shares / 4));
    }

    function test_joiningAndLeavingAtOnce_isNotProfitable() public {
        _seed(1000e18, 0);
        vm.prank(alice);
        pool.buy(false, 400e18, 0);
        uint256 before = usd.balanceOf(lp2);
        vm.startPrank(lp2);
        uint256 shares = pool.addLiquidity(100e18, 0, 0);
        pool.removeLiquidity(shares, 0, 0);
        // all YES/NO back as complete sets + leftovers; complete sets redeem 1:1
        uint256 sets = Math.min(yesT.balanceOf(lp2), noT.balanceOf(lp2));
        m.redeemSets(sets);
        vm.stopPrank();
        assertLe(usd.balanceOf(lp2), before, "can't extract collateral by adding and removing");
        assertGe(usd.balanceOf(lp2) + 2, before, "and loses at most rounding dust");
        assertLe(yesT.balanceOf(lp2) + noT.balanceOf(lp2), 2, "nothing but rounding dust left over");
    }

    // ---- trading ---------------------------------------------------------------------------

    function test_buyYes_raisesPrice_andTokensCostLessThanOneEach() public {
        _seed(1000e18, 0);
        (uint256 quoted, uint256 fee) = pool.quoteBuy(true, 100e18);
        vm.prank(alice);
        uint256 got = pool.buy(true, 100e18, quoted);
        assertEq(got, quoted);
        assertEq(fee, 2e18);
        assertEq(yesT.balanceOf(alice), got);
        assertGt(got, 100e18, "at 50% each YES costs less than 1");
        assertGt(pool.priceYes(), 0.5e18);
        // exact constant-product amount: invest 98 -> y' = 1000*1000/1098, out = 1098 - y'
        uint256 expected = 1098e18 - Math.mulDiv(1000e18, 1000e18, 1098e18, Math.Rounding.Ceil);
        assertEq(got, expected);
    }

    function test_buyNo_lowersYesPrice_pricesSumToOne() public {
        _seed(1000e18, 0);
        vm.prank(alice);
        pool.buy(false, 250e18, 0);
        (uint256 y, uint256 n) = pool.reserves();
        assertLt(pool.priceYes(), 0.5e18);
        assertEq(Math.mulDiv(n, 1e18, y + n) + Math.mulDiv(y, 1e18, y + n) + 1 >= 1e18, true);
    }

    function test_feesStayInThePool_productGrows() public {
        _seed(1000e18, 0);
        uint256 k0 = _k();
        vm.startPrank(alice);
        uint256 got = pool.buy(true, 100e18, 0);
        assertGt(_k(), k0, "buy fee grows y*n");
        uint256 k1 = _k();
        pool.sell(true, _maxSell(true, got), got);
        vm.stopPrank();
        assertGt(_k(), k1, "sell fee grows y*n");
    }

    function test_zeroFeePool_keepsProductAtLeastConstant() public {
        FPMM p0 = new FPMM(m, 0);
        vm.startPrank(lp);
        usd.approve(address(p0), type(uint256).max);
        p0.addLiquidity(1000e18, 0, 0);
        vm.stopPrank();
        (uint256 y0, uint256 n0) = p0.reserves();
        vm.startPrank(alice);
        usd.approve(address(p0), type(uint256).max);
        p0.buy(true, 123e18, 0);
        vm.stopPrank();
        (uint256 y1, uint256 n1) = p0.reserves();
        assertGe(y1 * n1, y0 * n0);
        assertLe(y1 * n1 - y0 * n0, n1, "only rounding, in the pool's favour");
    }

    function test_sellYes_paysCollateral_takesQuotedTokens() public {
        _seed(1000e18, 0);
        vm.startPrank(alice);
        uint256 got = pool.buy(true, 100e18, 0);
        (uint256 need,) = pool.quoteSell(true, 50e18);
        uint256 before = usd.balanceOf(alice);
        uint256 took = pool.sell(true, 50e18, need);
        vm.stopPrank();
        assertEq(took, need);
        assertEq(usd.balanceOf(alice) - before, 50e18);
        assertEq(yesT.balanceOf(alice), got - took);
        assertEq(usd.balanceOf(address(pool)), 0);
    }

    /// The sell side charges its fee too: the curve is evaluated as if out + fee sets left the pool.
    /// (The mutation check found that the first test suite didn't notice if it didn't.)
    function test_sellChargesTheFee_exactAmount() public {
        _seed(1000e18, 0);
        (uint256 tokensIn, uint256 fee) = pool.quoteSell(true, 98e18);
        assertEq(fee, 2e18, "98 out at 2% = 100 gross");
        // (1000 + t - 100) * (1000 - 100) = 1000 * 1000  =>  t = 1000*1000/900 + 100 - 1000
        uint256 expected = Math.mulDiv(1000e18, 1000e18, 900e18, Math.Rounding.Ceil) + 100e18 - 1000e18;
        assertEq(tokensIn, expected);
        // and it costs more tokens than in a zero-fee pool in the same state
        FPMM p0 = new FPMM(m, 0);
        vm.startPrank(lp2);
        usd.approve(address(p0), type(uint256).max);
        p0.addLiquidity(1000e18, 0, 0);
        vm.stopPrank();
        (uint256 noFeeIn,) = p0.quoteSell(true, 98e18);
        assertGt(tokensIn, noFeeIn);
    }

    function test_slippageLimitsAreEnforced() public {
        _seed(1000e18, 0);
        (uint256 q,) = pool.quoteBuy(true, 100e18);
        vm.startPrank(alice);
        vm.expectRevert(abi.encodeWithSelector(FPMM.Slippage.selector, q, q + 1));
        pool.buy(true, 100e18, q + 1);
        uint256 got = pool.buy(true, 100e18, q);
        (uint256 need,) = pool.quoteSell(true, 50e18);
        vm.expectRevert(abi.encodeWithSelector(FPMM.Slippage.selector, need, need - 1));
        pool.sell(true, 50e18, need - 1);
        vm.stopPrank();
        assertGt(got, 0);
    }

    function test_cannotSellMoreThanThePoolCanPay() public {
        _seed(100e18, 0);
        vm.expectRevert(FPMM.InsufficientLiquidity.selector);
        pool.quoteSell(true, 100e18);
    }

    function test_frontRunnerMovesPrice_slippageProtectsTheVictim() public {
        _seed(1000e18, 0);
        (uint256 q,) = pool.quoteBuy(true, 100e18);
        uint256 minOut = q * 99 / 100; // victim accepts 1% worse
        vm.prank(bob);
        pool.buy(true, 200e18, 0); // front-run
        vm.prank(alice);
        vm.expectRevert(); // Slippage
        pool.buy(true, 100e18, minOut);
    }

    // ---- lifecycle -------------------------------------------------------------------------

    function test_noTradingOrAddingAfterClose_butLiquidityCanLeave() public {
        uint256 shares = _seed(1000e18, 0);
        vm.prank(alice);
        uint256 got = pool.buy(true, 100e18, 0);
        vm.warp(closeAt);
        vm.startPrank(alice);
        vm.expectRevert(FPMM.MarketNotOpen.selector);
        pool.buy(true, 1e18, 0);
        vm.expectRevert(FPMM.MarketNotOpen.selector);
        pool.sell(true, 1e18, got);
        vm.stopPrank();
        vm.prank(lp2);
        vm.expectRevert(FPMM.MarketNotOpen.selector);
        pool.addLiquidity(1e18, 0, 0);
        vm.prank(lp);
        pool.removeLiquidity(shares, 0, 0);
        assertEq(pool.totalSupply(), pool.MINIMUM_LIQUIDITY(), "only the locked shares remain");
    }

    function test_fullLifecycle_winnerAndLpRedeem_marketStaysSolvent() public {
        uint256 shares = _seed(1000e18, 0);
        vm.prank(alice);
        uint256 aliceYes = pool.buy(true, 200e18, 0);
        vm.prank(bob);
        pool.buy(false, 100e18, 0);
        _settle(BinaryMarket.Outcome.Yes);

        vm.prank(alice);
        assertEq(m.redeem(), aliceYes, "each YES pays 1");
        vm.startPrank(lp);
        (uint256 yo,) = pool.removeLiquidity(shares, 0, 0);
        assertEq(m.redeem(), yo, "LP's YES pays 1, NO pays 0");
        vm.stopPrank();
        vm.prank(bob);
        assertEq(m.redeem(), 0, "NO lost");
        assertEq(m.yes().totalSupply(), yesT.balanceOf(address(pool)), "only the locked shares' dust is left");
        assertLt(m.yes().totalSupply(), 1000);
        assertGe(usd.balanceOf(address(m)), m.liability());
    }

    function test_feeOnTransferCollateral_cannotTrade() public {
        FeeToken f = new FeeToken();
        BinaryMarket fm = new BinaryMarket(IERC20(address(f)), "q", closeAt, 1 days, 0, resolver, arbiter);
        FPMM fp = new FPMM(fm, FEE);
        f.mint(alice, 1000e18);
        vm.startPrank(alice);
        f.approve(address(fp), type(uint256).max);
        fp.addLiquidity(500e18, 0, 0); // liquidity works: shares are minted for what arrived
        vm.expectRevert(FPMM.BadConfig.selector);
        fp.buy(true, 10e18, 0);
        vm.stopPrank();
    }

    function test_constructorRejectsBadConfig() public {
        vm.expectRevert(FPMM.BadConfig.selector);
        new FPMM(m, 1001);
        vm.expectRevert();
        new FPMM(BinaryMarket(address(0)), 0);
    }

    // ---- the trade-off, in numbers -----------------------------------------------------------

    /// The cost of always-on liquidity: an informed trader who knows YES will win buys until the
    /// price is ~93%, and the LP is left holding mostly NO. Deterministic, so these numbers are
    /// the same on every machine.
    function test_tradeoff_informedTraderVsLp() public {
        _seed(1000e18, 0);
        vm.prank(alice);
        uint256 got = pool.buy(true, 3300e18, 0);
        uint256 priceAfter = pool.priceYes();
        _settle(BinaryMarket.Outcome.Yes);

        vm.prank(alice);
        uint256 alicePaid = m.redeem();
        vm.startPrank(lp);
        pool.removeLiquidity(pool.balanceOf(lp), 0, 0);
        uint256 lpGot = m.redeem();
        vm.stopPrank();

        console.log("price of YES after the informed buy (bps):", priceAfter * 10_000 / 1e18);
        console.log("informed trader: paid 3300, YES received:", got / 1e18, "redeemed:", alicePaid / 1e18);
        console.log("LP: deposited 1000, got back:", lpGot / 1e18);
        assertGt(priceAfter, 0.93e18);
        assertLt(lpGot, 1000e18, "LP loses to informed flow");
        // zero-sum: every unit is paid to someone, except dust behind the 1000 locked shares
        assertApproxEqAbs(alicePaid + lpGot, 3300e18 + 1000e18, 1000);
    }

    /// The other side: uninformed two-way flow pays fees to the LP.
    function test_tradeoff_noiseTradersPayTheLp() public {
        uint256 shares = _seed(1000e18, 0);
        for (uint256 i = 0; i < 20; i++) {
            address t = i % 2 == 0 ? alice : bob;
            bool side = i % 4 < 2;
            vm.startPrank(t);
            uint256 got = pool.buy(side, 50e18, 0);
            pool.sell(side, _maxSell(side, got), got);
            vm.stopPrank();
        }
        // Value of the LP's position if the market resolved Invalid (0.5 per token): fair either way
        // because the pool is back near 50/50.
        (uint256 y, uint256 n) = pool.reserves();
        uint256 lpValue = (y + n) / 2 * shares / pool.totalSupply();
        console.log("LP position after 20 round trips of 50 (Invalid value):", lpValue / 1e18);
        assertGt(lpValue, 1000e18, "fees from round trips accrue to the LP");
    }

    // ---- fuzz --------------------------------------------------------------------------------

    function testFuzz_tradesNeverReduceProduct(uint256 amt, bool isYes, uint256 seedAmt) public {
        seedAmt = bound(seedAmt, 1e18, 1_000_000e18);
        amt = bound(amt, 1, 500_000e18);
        _seed(seedAmt, 0);
        uint256 k0 = _k();
        vm.prank(alice);
        uint256 got = pool.buy(isYes, amt, 0);
        assertGe(_k(), k0);
        uint256 k1 = _k();
        uint256 out = _maxSell(isYes, got);
        if (out > 0) {
            vm.prank(alice);
            pool.sell(isYes, out, got);
            assertGe(_k(), k1);
        }
        assertEq(usd.balanceOf(address(pool)), 0);
    }

    /// Buying and selling straight back never returns more collateral than was paid.
    function testFuzz_roundTripNeverProfits(uint256 amt, bool isYes, uint16 probBps) public {
        amt = bound(amt, 1e6, 200_000e18);
        _seed(10_000e18, bound(probBps, 100, 9900));
        uint256 before = usd.balanceOf(alice);
        vm.startPrank(alice);
        uint256 got = pool.buy(isYes, amt, 0);
        uint256 out = _maxSell(isYes, got);
        if (out > 0) pool.sell(isYes, out, got);
        vm.stopPrank();
        assertLe(usd.balanceOf(alice), before);
    }

    function testFuzz_quoteMatchesExecution(uint256 amt, bool isYes) public {
        _seed(5000e18, 0);
        amt = bound(amt, 1, 100_000e18);
        (uint256 q,) = pool.quoteBuy(isYes, amt);
        vm.prank(alice);
        assertEq(pool.buy(isYes, amt, q), q);
    }
}

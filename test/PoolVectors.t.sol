// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BinaryMarket} from "../src/BinaryMarket.sol";
import {FixedProductMarketMaker as FPMM} from "../src/FixedProductMarketMaker.sol";
import {MockERC20} from "./utils/MockERC20.sol";

/// Fixed quotes that analysis/pool.py (the dashboard's Python copy of the pool maths) must also
/// produce: analysis/tests/test_pool.py checks the same numbers. If either side changes its
/// rounding, one of the two tests fails.
contract PoolVectorsTest is Test {
    BinaryMarket internal m;
    MockERC20 internal usd;
    address internal lp = makeAddr("lp");
    address internal alice = makeAddr("alice");

    function _pool(uint256 feeBps, uint256 amount, uint256 probBps) internal returns (FPMM pool) {
        usd = new MockERC20();
        m = new BinaryMarket(IERC20(address(usd)), "q", uint64(block.timestamp + 7 days), 2 days, 100e18, makeAddr("r"), makeAddr("a"));
        pool = new FPMM(m, feeBps);
        address[2] memory users = [lp, alice];
        for (uint256 i = 0; i < 2; i++) {
            usd.mint(users[i], 1_000_000e18);
            vm.startPrank(users[i]);
            usd.approve(address(pool), type(uint256).max);
            IERC20(address(m.yes())).approve(address(pool), type(uint256).max);
            IERC20(address(m.no())).approve(address(pool), type(uint256).max);
            vm.stopPrank();
        }
        vm.prank(lp);
        pool.addLiquidity(amount, probBps, 0);
    }

    function test_vectors_70pct_2pctFee() public {
        FPMM pool = _pool(200, 1000e18, 7000);
        (uint256 y, uint256 n) = pool.reserves();
        assertEq(y, 428571428571428571428);
        assertEq(n, 1000000000000000000000);
        assertEq(pool.priceYes(), 700000000000000000);

        (uint256 out, uint256 fee) = pool.quoteBuy(true, 100e18);
        assertEq(out, 136251366120218579234);
        assertEq(fee, 2000000000000000000);
        (out, fee) = pool.quoteBuy(false, 37.5e18);
        assertEq(out, 115727665208381303246);
        assertEq(fee, 750000000000000000);
        // fees that aren't whole numbers: the contract rounds them UP
        (out, fee) = pool.quoteBuy(true, 7e18 + 3);
        assertEq(out, 9779969012573744118);
        assertEq(fee, 140000000000000001);
        (out, fee) = pool.quoteBuy(false, 123456789);
        assertEq(out, 403292176);
        assertEq(fee, 2469136);

        vm.prank(alice);
        pool.buy(true, 100e18, 0);
        (y, n) = pool.reserves();
        assertEq(y, 392320062451209992194);
        assertEq(n, 1100000000000000000000);
        assertEq(pool.priceYes(), 737107291979439880);

        (uint256 tokensIn, uint256 sellFee) = pool.quoteSell(true, 50e18);
        assertEq(tokensIn, 70102123263032329090);
        assertEq(sellFee, 1020408163265306123);
    }

    function test_vectors_25pct_noFee() public {
        FPMM pool = _pool(0, 500e18, 2500);
        (uint256 y, uint256 n) = pool.reserves();
        assertEq(y, 500000000000000000000);
        assertEq(n, 166666666666666666666);
        assertEq(pool.priceYes(), 249999999999999999);
        (uint256 out, uint256 fee) = pool.quoteBuy(true, 7e18 + 3);
        assertEq(out, 27153550863723608456);
        assertEq(fee, 0);
    }
}

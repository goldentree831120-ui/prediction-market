// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BinaryMarket} from "../../src/BinaryMarket.sol";
import {FixedProductMarketMaker} from "../../src/FixedProductMarketMaker.sol";
import {MockERC20} from "../utils/MockERC20.sol";
import {PoolHandler} from "./PoolHandler.sol";

contract FixedProductMarketMakerInvariantTest is Test {
    BinaryMarket internal m;
    FixedProductMarketMaker internal pool;
    MockERC20 internal usd;
    PoolHandler internal h;

    function setUp() public {
        usd = new MockERC20();
        address resolver = makeAddr("resolver");
        m = new BinaryMarket(
            IERC20(address(usd)), "q", uint64(block.timestamp + 60 days), 1 days, 0, resolver, makeAddr("arbiter")
        );
        pool = new FixedProductMarketMaker(m, 200);
        address[] memory users = new address[](4);
        for (uint256 i = 0; i < 4; i++) {
            users[i] = makeAddr(string.concat("user", vm.toString(i)));
            usd.mint(users[i], 10_000_000e18);
            vm.startPrank(users[i]);
            usd.approve(address(pool), type(uint256).max);
            usd.approve(address(m), type(uint256).max);
            IERC20(address(m.yes())).approve(address(pool), type(uint256).max);
            IERC20(address(m.no())).approve(address(pool), type(uint256).max);
            vm.stopPrank();
        }
        // Seed liquidity so trades have something to trade against from the first call.
        vm.prank(users[0]);
        pool.addLiquidity(10_000e18, 0, 0);
        h = new PoolHandler(m, pool, usd, resolver, users);
        targetContract(address(h));
    }

    /// Trades, fees and liquidity moves never reduce what one liquidity share is worth
    /// (sqrt(y * n) per share): every rounding goes in the pool's favour.
    function invariant_shareValueNeverDecreases() public view {
        assertEq(h.shareValueDrops(), 0);
    }

    /// The pool never sits on loose collateral: everything it receives becomes complete sets.
    function invariant_poolHoldsNoCollateral() public view {
        assertEq(usd.balanceOf(address(pool)), 0);
    }

    /// The M1 invariant still holds with the pool trading: the market can pay every token holder.
    function invariant_marketStaysSolvent() public view {
        assertGe(usd.balanceOf(address(m)), m.liability() + m.bondHeld());
    }

    /// While anyone holds shares, both sides of the pool are non-empty (the price stays strictly
    /// between 0 and 1), and before resolution YES and NO still come only in pairs.
    function invariant_poolNeverRunsDry() public view {
        (uint256 y, uint256 n) = pool.reserves();
        if (pool.totalSupply() > 0) {
            assertGt(y, 0);
            assertGt(n, 0);
        }
        if (!m.finalized()) assertEq(m.yes().totalSupply(), m.no().totalSupply());
    }

    function afterInvariant() public view {
        console.log("trades:", h.trades(), "liquidity ops:", h.liquidityOps());
        console.log("resolved:", h.resolved());
    }
}

// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BinaryMarket} from "../../src/BinaryMarket.sol";
import {MockERC20} from "../utils/MockERC20.sol";
import {MarketHandler} from "./MarketHandler.sol";

contract BinaryMarketInvariantTest is Test {
    BinaryMarket internal m;
    MockERC20 internal usd;
    MarketHandler internal h;

    function setUp() public {
        usd = new MockERC20();
        address resolver = makeAddr("resolver");
        address arbiter = makeAddr("arbiter");
        m = new BinaryMarket(
            IERC20(address(usd)), "q", uint64(block.timestamp + 5 days), 2 days, 50e18, resolver, arbiter
        );
        address[] memory users = new address[](4);
        for (uint256 i = 0; i < 4; i++) {
            users[i] = makeAddr(string.concat("user", vm.toString(i)));
            usd.mint(users[i], 1_000_000e18);
            vm.prank(users[i]);
            usd.approve(address(m), type(uint256).max);
        }
        h = new MarketHandler(m, usd, resolver, arbiter, users);
        targetContract(address(h));
    }

    /// THE invariant from the brief: collateral always covers what the market owes.
    /// liability() = what outcome-token holders could claim right now; bonds are held on top.
    function invariant_collateralCoversOutstandingSets() public view {
        assertGe(usd.balanceOf(address(m)), m.liability() + m.bondHeld());
    }

    /// Before resolution, YES and NO only ever come into or leave existence together.
    function invariant_setsStayPairedUntilFinal() public view {
        if (!m.finalized()) assertEq(m.yes().totalSupply(), m.no().totalSupply());
    }

    /// Exact accounting: balance = in - out, nothing created or lost.
    function invariant_collateralAccounting() public view {
        assertEq(
            usd.balanceOf(address(m)),
            h.deposited() + h.bondsIn() - h.setsRedeemed() - h.paidOut() - h.bondsOut()
        );
    }

    /// Nobody was ever paid more than their tokens were worth under the final outcome.
    function invariant_noOverpayment() public view {
        assertEq(h.payoutsBeyondTokens(), 0);
        assertLe(h.paidOut() + h.setsRedeemed(), h.deposited());
    }

    /// Rounding only ever strands dust in the market, at most 1 wei per Invalid redemption.
    function invariant_roundingDustIsBounded() public view {
        uint256 held = usd.balanceOf(address(m)) - m.bondHeld();
        assertLe(held - m.liability(), h.invalidRedemptions() + 1);
    }

    function afterInvariant() public view {
        console.log("phase:", uint8(m.phase()), "deposited:", h.deposited() / 1e18);
        console.log("paid out:", h.paidOut() / 1e18, "sets redeemed:", h.setsRedeemed() / 1e18);
    }
}

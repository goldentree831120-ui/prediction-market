// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {CommonBase} from "forge-std/Base.sol";
import {StdCheats} from "forge-std/StdCheats.sol";
import {StdUtils} from "forge-std/StdUtils.sol";
import {BinaryMarket} from "../../src/BinaryMarket.sol";
import {OutcomeToken} from "../../src/OutcomeToken.sol";
import {MockERC20} from "../utils/MockERC20.sol";

/// Random users mint, trade outcome tokens between themselves, redeem sets, and the
/// resolver / disputers / arbiter move the market through its lifecycle at random.
/// Ghost variables track every unit of collateral that went in or out.
contract MarketHandler is CommonBase, StdCheats, StdUtils {
    BinaryMarket public immutable m;
    MockERC20 public immutable usd;
    OutcomeToken public immutable yes;
    OutcomeToken public immutable no;
    address public immutable resolver;
    address public immutable arbiter;
    address[] public users;

    uint256 public deposited; // collateral in via mintSets
    uint256 public setsRedeemed; // collateral out via redeemSets
    uint256 public paidOut; // collateral out via redeem
    uint256 public invalidRedemptions; // each can strand at most 1 wei of rounding dust
    uint256 public bondsIn;
    uint256 public bondsOut;
    uint256 public payoutsBeyondTokens; // a redeem that paid more than the tokens were worth

    constructor(BinaryMarket m_, MockERC20 usd_, address resolver_, address arbiter_, address[] memory users_) {
        m = m_;
        usd = usd_;
        yes = m_.yes();
        no = m_.no();
        resolver = resolver_;
        arbiter = arbiter_;
        users = users_;
    }

    function mintSets(uint256 who, uint256 amt) external {
        address u = _user(who);
        amt = bound(amt, 1, 1_000e18);
        vm.prank(u);
        try m.mintSets(amt) {
            deposited += amt;
        } catch {}
    }

    function redeemSets(uint256 who, uint256 amt) external {
        address u = _user(who);
        uint256 max = _min(yes.balanceOf(u), no.balanceOf(u));
        if (max == 0) return;
        amt = bound(amt, 1, max);
        vm.prank(u);
        m.redeemSets(amt);
        setsRedeemed += amt;
    }

    /// Outcome tokens change hands; this is what makes holders' YES/NO balances uneven.
    function trade(uint256 from, uint256 to, uint256 amt, bool sideYes) external {
        address a = _user(from);
        address b = _user(to);
        OutcomeToken t = sideYes ? yes : no;
        uint256 bal = t.balanceOf(a);
        if (bal == 0) return;
        vm.prank(a);
        t.transfer(b, bound(amt, 1, bal));
    }

    function warp(uint256 secs) external {
        skip(bound(secs, 1, 2 days)); // short steps so most runs mint and trade before close
    }

    function propose(uint8 o) external {
        vm.prank(resolver);
        try m.proposeOutcome(BinaryMarket.Outcome(bound(o, 1, 3))) {} catch {}
    }

    function dispute(uint256 who) external {
        address u = _user(who);
        uint256 bond = m.disputeBond();
        usd.mint(u, bond);
        vm.prank(u);
        try m.dispute() {
            bondsIn += bond;
        } catch {}
    }

    function arbitrate(uint8 o) external {
        uint256 before = m.bondHeld();
        vm.prank(arbiter);
        try m.arbitrate(BinaryMarket.Outcome(bound(o, 1, 3))) {
            bondsOut += before;
        } catch {}
    }

    function finalize() external {
        try m.finalize() {} catch {}
    }

    function finalizeAsInvalid() external {
        uint256 before = m.bondHeld();
        try m.finalizeAsInvalid() {
            bondsOut += before;
        } catch {}
    }

    function redeem(uint256 who) external {
        address u = _user(who);
        uint256 y = yes.balanceOf(u);
        uint256 n = no.balanceOf(u);
        uint256 before = usd.balanceOf(u);
        vm.prank(u);
        try m.redeem() returns (uint256 p) {
            uint256 got = usd.balanceOf(u) - before;
            paidOut += got;
            // Independent check of the payout rule, not using m.payoutFor.
            uint256 maxFair = m.finalOutcome() == BinaryMarket.Outcome.Yes
                ? y
                : m.finalOutcome() == BinaryMarket.Outcome.No ? n : (y + n) / 2;
            if (got > maxFair || p != got) payoutsBeyondTokens++;
            if (m.finalOutcome() == BinaryMarket.Outcome.Invalid) invalidRedemptions++;
        } catch {}
    }

    function _user(uint256 seed) internal view returns (address) {
        return users[seed % users.length];
    }

    function _min(uint256 a, uint256 b) internal pure returns (uint256) {
        return a < b ? a : b;
    }
}

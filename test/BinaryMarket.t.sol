// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BinaryMarket} from "../src/BinaryMarket.sol";
import {OutcomeToken} from "../src/OutcomeToken.sol";
import {MockERC20, FeeToken} from "./utils/MockERC20.sol";

contract BinaryMarketTest is Test {
    BinaryMarket internal m;
    MockERC20 internal usd;

    address internal resolver = makeAddr("resolver");
    address internal arbiter = makeAddr("arbiter");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal carol = makeAddr("carol");

    uint64 internal closeAt;
    uint64 internal constant WINDOW = 2 days;
    uint256 internal constant BOND = 100e18;

    function setUp() public {
        usd = new MockERC20();
        closeAt = uint64(block.timestamp + 7 days);
        m = new BinaryMarket(IERC20(address(usd)), "Will X happen by date Y?", closeAt, WINDOW, BOND, resolver, arbiter);
        address[3] memory users = [alice, bob, carol];
        for (uint256 i = 0; i < 3; i++) {
            usd.mint(users[i], 10_000e18);
            vm.prank(users[i]);
            usd.approve(address(m), type(uint256).max);
        }
    }

    function _mint(address who, uint256 amt) internal {
        vm.prank(who);
        m.mintSets(amt);
    }

    function _closeAndPropose(BinaryMarket.Outcome o) internal {
        vm.warp(closeAt);
        vm.prank(resolver);
        m.proposeOutcome(o);
    }

    function _settle(BinaryMarket.Outcome o) internal {
        _closeAndPropose(o);
        skip(WINDOW);
        m.finalize();
    }

    // ------------------------------------------------------------------
    // Complete sets
    // ------------------------------------------------------------------

    function test_mintSets_givesOneYesAndOneNoPerCollateral() public {
        _mint(alice, 100e18);
        assertEq(m.yes().balanceOf(alice), 100e18);
        assertEq(m.no().balanceOf(alice), 100e18);
        assertEq(usd.balanceOf(address(m)), 100e18);
        assertEq(m.yes().decimals(), 18);
    }

    function test_redeemSets_returnsCollateral_inAnyPhase() public {
        _mint(alice, 100e18);
        vm.prank(alice);
        m.redeemSets(40e18);
        assertEq(usd.balanceOf(alice), 10_000e18 - 60e18);

        _settle(BinaryMarket.Outcome.No);
        vm.prank(alice);
        m.redeemSets(60e18); // still exactly 1 per set after resolution
        assertEq(usd.balanceOf(alice), 10_000e18);
    }

    function test_cannotRedeemSetWithoutBothSides() public {
        _mint(alice, 10e18);
        OutcomeToken y = m.yes(); // read first: vm.prank applies to the NEXT call only
        vm.prank(alice);
        y.transfer(bob, 10e18);
        vm.prank(alice);
        vm.expectRevert(); // ERC20InsufficientBalance on YES
        m.redeemSets(1);
    }

    function test_cannotMintAfterClose() public {
        vm.warp(closeAt);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(BinaryMarket.WrongPhase.selector, BinaryMarket.Phase.Open, BinaryMarket.Phase.Closed));
        m.mintSets(1);
    }

    function test_onlyMarketCanMintOrBurnOutcomeTokens() public {
        OutcomeToken y = m.yes();
        vm.expectRevert(OutcomeToken.OnlyMarket.selector);
        y.mint(alice, 1);
        vm.expectRevert(OutcomeToken.OnlyMarket.selector);
        y.burn(alice, 1);
    }

    function test_feeOnTransferCollateral_mintsOnlyWhatArrived() public {
        FeeToken fee = new FeeToken();
        BinaryMarket fm =
            new BinaryMarket(IERC20(address(fee)), "q", uint64(block.timestamp + 1 days), WINDOW, BOND, resolver, arbiter);
        fee.mint(alice, 100e18);
        vm.startPrank(alice);
        fee.approve(address(fm), type(uint256).max);
        fm.mintSets(100e18);
        vm.stopPrank();
        assertEq(fm.yes().balanceOf(alice), 99e18);
        assertEq(fee.balanceOf(address(fm)), 99e18);
    }

    // ------------------------------------------------------------------
    // Resolution
    // ------------------------------------------------------------------

    function test_onlyResolverProposes_onlyAfterClose() public {
        vm.prank(resolver);
        vm.expectRevert(abi.encodeWithSelector(BinaryMarket.WrongPhase.selector, BinaryMarket.Phase.Closed, BinaryMarket.Phase.Open));
        m.proposeOutcome(BinaryMarket.Outcome.Yes);

        vm.warp(closeAt);
        vm.prank(alice);
        vm.expectRevert(BinaryMarket.NotResolver.selector);
        m.proposeOutcome(BinaryMarket.Outcome.Yes);

        vm.prank(resolver);
        vm.expectRevert(BinaryMarket.BadOutcome.selector);
        m.proposeOutcome(BinaryMarket.Outcome.Unresolved);
    }

    function test_cannotFinalizeDuringWindow_canAfter() public {
        _closeAndPropose(BinaryMarket.Outcome.Yes);
        skip(WINDOW - 1);
        vm.expectRevert(BinaryMarket.DisputeWindowOpen.selector);
        m.finalize();
        skip(1);
        m.finalize();
        assertEq(uint8(m.phase()), uint8(BinaryMarket.Phase.Final));
        assertEq(uint8(m.finalOutcome()), uint8(BinaryMarket.Outcome.Yes));
    }

    function test_cannotDisputeAfterWindow() public {
        _closeAndPropose(BinaryMarket.Outcome.Yes);
        skip(WINDOW);
        vm.prank(bob);
        vm.expectRevert(BinaryMarket.DisputeWindowClosed.selector);
        m.dispute();
    }

    function test_disputeOverturned_bondReturnedToDisputer() public {
        _closeAndPropose(BinaryMarket.Outcome.Yes);
        vm.prank(bob);
        m.dispute();
        assertEq(m.bondHeld(), BOND);
        vm.expectRevert(abi.encodeWithSelector(BinaryMarket.WrongPhase.selector, BinaryMarket.Phase.Proposed, BinaryMarket.Phase.Disputed));
        m.finalize(); // a disputed proposal can't be finalized by anyone

        vm.prank(alice);
        vm.expectRevert(BinaryMarket.NotArbiter.selector);
        m.arbitrate(BinaryMarket.Outcome.No);

        vm.prank(arbiter);
        m.arbitrate(BinaryMarket.Outcome.No);
        assertEq(usd.balanceOf(bob), 10_000e18);
        assertEq(m.bondHeld(), 0);
        assertEq(uint8(m.finalOutcome()), uint8(BinaryMarket.Outcome.No));
    }

    function test_disputeUpheld_bondGoesToResolver() public {
        _closeAndPropose(BinaryMarket.Outcome.Yes);
        vm.prank(bob);
        m.dispute();
        vm.prank(arbiter);
        m.arbitrate(BinaryMarket.Outcome.Yes);
        assertEq(usd.balanceOf(bob), 10_000e18 - BOND);
        assertEq(usd.balanceOf(resolver), BOND);
    }

    function test_silentResolver_anyoneFinalizesInvalidAfterTimeout() public {
        _mint(alice, 10e18);
        vm.warp(closeAt + m.RESOLUTION_TIMEOUT() - 1);
        vm.expectRevert(BinaryMarket.TooEarly.selector);
        m.finalizeAsInvalid();
        skip(1);
        m.finalizeAsInvalid();
        vm.prank(alice);
        assertEq(m.redeem(), 10e18); // 10 YES + 10 NO at 0.5 each
    }

    function test_silentArbiter_finalizesInvalid_andReturnsBond() public {
        _closeAndPropose(BinaryMarket.Outcome.Yes);
        vm.prank(bob);
        m.dispute();
        skip(WINDOW + m.ARBITRATION_TIMEOUT());
        m.finalizeAsInvalid();
        assertEq(usd.balanceOf(bob), 10_000e18);
        assertEq(uint8(m.finalOutcome()), uint8(BinaryMarket.Outcome.Invalid));
    }

    function test_finalizeAsInvalid_notAllowedOnceProposed() public {
        _closeAndPropose(BinaryMarket.Outcome.Yes);
        skip(365 days);
        vm.expectRevert(abi.encodeWithSelector(BinaryMarket.WrongPhase.selector, BinaryMarket.Phase.Closed, BinaryMarket.Phase.Proposed));
        m.finalizeAsInvalid(); // an undisputed proposal finalizes normally instead
    }

    // ------------------------------------------------------------------
    // Payout
    // ------------------------------------------------------------------

    function test_yesWins_yesPaysOne_noPaysZero() public {
        _mint(alice, 100e18);
        OutcomeToken n = m.no();
        vm.prank(alice);
        n.transfer(bob, 100e18); // alice holds YES only, bob NO only
        _settle(BinaryMarket.Outcome.Yes);

        vm.prank(alice);
        assertEq(m.redeem(), 100e18);
        vm.prank(bob);
        assertEq(m.redeem(), 0);
        assertEq(n.balanceOf(bob), 0); // losing tokens are burned too
        assertEq(usd.balanceOf(address(m)), 0);
    }

    function test_invalid_paysHalfEach_roundsDown() public {
        _mint(alice, 3); // 3 wei sets
        OutcomeToken n = m.no();
        vm.prank(alice);
        n.transfer(bob, 3);
        _settle(BinaryMarket.Outcome.Invalid);
        vm.prank(alice);
        assertEq(m.redeem(), 1); // 3 * 0.5 = 1.5 -> 1
        vm.prank(bob);
        assertEq(m.redeem(), 1);
        assertEq(usd.balanceOf(address(m)), 1); // 1 wei of dust stays: never over-paid
    }

    function test_cannotRedeemBeforeFinal() public {
        _mint(alice, 1e18);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(BinaryMarket.WrongPhase.selector, BinaryMarket.Phase.Final, BinaryMarket.Phase.Open));
        m.redeem();
    }

    function test_constructorRejectsBadConfig() public {
        vm.expectRevert(BinaryMarket.BadConfig.selector);
        new BinaryMarket(IERC20(address(usd)), "q", uint64(block.timestamp), WINDOW, BOND, resolver, arbiter);
        vm.expectRevert(BinaryMarket.BadConfig.selector);
        new BinaryMarket(IERC20(address(usd)), "q", closeAt, WINDOW, BOND, resolver, resolver);
        vm.expectRevert(BinaryMarket.BadConfig.selector);
        new BinaryMarket(IERC20(address(usd)), "q", closeAt, 0, BOND, resolver, arbiter);
    }

    // ------------------------------------------------------------------
    // Fuzz
    // ------------------------------------------------------------------

    /// Any split of YES/NO between two holders, any outcome: total paid <= total deposited,
    /// and for Yes/No the winners get exactly what was deposited.
    function testFuzz_payoutNeverExceedsDeposits(uint256 amt, uint256 split, uint8 o) public {
        amt = bound(amt, 1, 10_000e18);
        split = bound(split, 0, amt);
        BinaryMarket.Outcome outcome = BinaryMarket.Outcome(bound(o, 1, 3));
        _mint(alice, amt);
        vm.startPrank(alice);
        m.yes().transfer(bob, split);
        m.no().transfer(bob, amt - split);
        vm.stopPrank();
        _settle(outcome);

        vm.prank(alice);
        uint256 pa = m.redeem();
        vm.prank(bob);
        uint256 pb = m.redeem();
        assertLe(pa + pb, amt);
        if (outcome != BinaryMarket.Outcome.Invalid) assertEq(pa + pb, amt);
        else assertGe(pa + pb + 1, amt); // at most 1 wei lost to rounding per holder pair
    }
}

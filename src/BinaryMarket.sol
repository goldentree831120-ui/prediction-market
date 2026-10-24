// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {OutcomeToken} from "./OutcomeToken.sol";

/// @title BinaryMarket
/// @notice One yes/no question, collateralised 1:1.
///
///   Complete sets: deposit 1 collateral -> receive 1 YES + 1 NO. Return 1 YES + 1 NO
///   -> get 1 collateral back, at any time. Because a set is always worth exactly 1
///   collateral, the market never needs a price to stay solvent; prices only matter
///   for trading YES against NO (M2).
///
///   Lifecycle:
///     Open      trading and minting until `closeTime`
///     Closed    after closeTime; the resolver may propose an outcome
///     Proposed  dispute window running; anyone may dispute by posting a bond
///     Disputed  the arbiter decides the final outcome
///     Final     holders redeem: winner pays 1, loser 0; Invalid pays 0.5 to each side
///
///   If the resolver or the arbiter goes silent, `finalizeAsInvalid` releases funds after a timeout.
///
///   Roles are fixed at deployment (no admin, no upgrades). The resolver and arbiter
///   are trusted to report honestly; the dispute window and bond limit how cheaply a
///   wrong report can stick, they do not remove the trust. See README.
contract BinaryMarket is ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum Outcome {
        Unresolved,
        Yes,
        No,
        Invalid
    }

    enum Phase {
        Open,
        Closed,
        Proposed,
        Disputed,
        Final
    }

    uint256 public constant RESOLUTION_TIMEOUT = 30 days; // after closeTime, if nobody proposes
    uint256 public constant ARBITRATION_TIMEOUT = 30 days; // after the window, if the arbiter never rules

    IERC20 public immutable collateral;
    OutcomeToken public immutable yes;
    OutcomeToken public immutable no;

    string public question;
    uint64 public immutable closeTime;
    uint64 public immutable disputeWindow;
    uint256 public immutable disputeBond;
    address public immutable resolver;
    address public immutable arbiter;

    Outcome public proposedOutcome;
    uint64 public proposedAt;
    Outcome public finalOutcome;
    address public disputer;
    bool public finalized;
    /// Collateral held as a dispute bond, NOT backing outcome tokens.
    uint256 public bondHeld;

    event SetsMinted(address indexed account, uint256 amount);
    event SetsRedeemed(address indexed account, uint256 amount);
    event OutcomeProposed(Outcome outcome);
    event Disputed(address indexed disputer, uint256 bond);
    event Finalized(Outcome outcome, bool viaArbiter);
    event PaidOut(address indexed account, uint256 yesBurned, uint256 noBurned, uint256 payout);

    error WrongPhase(Phase expected, Phase actual);
    error NotResolver();
    error NotArbiter();
    error ZeroAmount();
    error BadOutcome();
    error BadConfig();
    error DisputeWindowOpen();
    error DisputeWindowClosed();
    error TooEarly();

    constructor(
        IERC20 collateral_,
        string memory question_,
        uint64 closeTime_,
        uint64 disputeWindow_,
        uint256 disputeBond_,
        address resolver_,
        address arbiter_
    ) {
        if (
            closeTime_ <= block.timestamp || disputeWindow_ == 0 || resolver_ == address(0)
                || arbiter_ == address(0) || resolver_ == arbiter_
        ) revert BadConfig();
        collateral = collateral_;
        question = question_;
        closeTime = closeTime_;
        disputeWindow = disputeWindow_;
        disputeBond = disputeBond_;
        resolver = resolver_;
        arbiter = arbiter_;
        uint8 dec = IERC20Metadata(address(collateral_)).decimals();
        yes = new OutcomeToken("YES", "YES", dec);
        no = new OutcomeToken("NO", "NO", dec);
    }

    // ------------------------------------------------------------------
    // Complete sets
    // ------------------------------------------------------------------

    /// @notice Deposit `amount` collateral, receive `amount` YES and `amount` NO. Only while Open.
    function mintSets(uint256 amount) external nonReentrant {
        _expect(Phase.Open);
        if (amount == 0) revert ZeroAmount();
        // Pull first, then mint: if the transfer fails nothing was created.
        // Measured by balance difference, so a fee-on-transfer token can't make us
        // mint more sets than collateral actually arrived.
        uint256 before = collateral.balanceOf(address(this));
        collateral.safeTransferFrom(msg.sender, address(this), amount);
        uint256 received = collateral.balanceOf(address(this)) - before;
        yes.mint(msg.sender, received);
        no.mint(msg.sender, received);
        emit SetsMinted(msg.sender, received);
    }

    /// @notice Burn `amount` YES + `amount` NO, get `amount` collateral. Allowed in every phase:
    ///         a complete set is worth exactly 1 whatever the outcome.
    function redeemSets(uint256 amount) external nonReentrant {
        if (amount == 0) revert ZeroAmount();
        yes.burn(msg.sender, amount);
        no.burn(msg.sender, amount);
        collateral.safeTransfer(msg.sender, amount);
        emit SetsRedeemed(msg.sender, amount);
    }

    // ------------------------------------------------------------------
    // Resolution
    // ------------------------------------------------------------------

    function proposeOutcome(Outcome outcome) external {
        if (msg.sender != resolver) revert NotResolver();
        _expect(Phase.Closed);
        if (outcome == Outcome.Unresolved) revert BadOutcome();
        proposedOutcome = outcome;
        proposedAt = uint64(block.timestamp);
        emit OutcomeProposed(outcome);
    }

    /// @notice Anyone can dispute the proposed outcome during the window by posting `disputeBond`.
    function dispute() external nonReentrant {
        _expect(Phase.Proposed);
        if (block.timestamp >= proposedAt + disputeWindow) revert DisputeWindowClosed();
        disputer = msg.sender;
        bondHeld = disputeBond;
        collateral.safeTransferFrom(msg.sender, address(this), disputeBond);
        emit Disputed(msg.sender, disputeBond);
    }

    /// @notice After an undisputed window, anyone can finalize the proposed outcome.
    function finalize() external {
        _expect(Phase.Proposed);
        if (block.timestamp < proposedAt + disputeWindow) revert DisputeWindowOpen();
        _finalize(proposedOutcome, false);
    }

    /// @notice Liveness fallback. If the resolver never proposes, or the arbiter never rules,
    ///         funds must not be locked forever: after the timeout anyone can finalize as
    ///         Invalid (everyone gets 0.5 per token) and a pending dispute bond is returned.
    function finalizeAsInvalid() external nonReentrant {
        Phase p = phase();
        if (p == Phase.Closed) {
            if (block.timestamp < uint256(closeTime) + RESOLUTION_TIMEOUT) revert TooEarly();
        } else if (p == Phase.Disputed) {
            if (block.timestamp < uint256(proposedAt) + disputeWindow + ARBITRATION_TIMEOUT) revert TooEarly();
        } else {
            revert WrongPhase(Phase.Closed, p);
        }
        _finalize(Outcome.Invalid, false);
        uint256 bond = bondHeld;
        bondHeld = 0;
        if (bond > 0) collateral.safeTransfer(disputer, bond);
    }

    /// @notice The arbiter settles a dispute. If the arbiter disagrees with the proposal,
    ///         the disputer gets the bond back; otherwise the bond goes to the resolver,
    ///         who had to defend a correct answer.
    function arbitrate(Outcome outcome) external nonReentrant {
        if (msg.sender != arbiter) revert NotArbiter();
        _expect(Phase.Disputed);
        if (outcome == Outcome.Unresolved) revert BadOutcome();
        _finalize(outcome, true);
        uint256 bond = bondHeld;
        bondHeld = 0;
        address to = outcome == proposedOutcome ? resolver : disputer;
        if (bond > 0) collateral.safeTransfer(to, bond);
    }

    // ------------------------------------------------------------------
    // Payout
    // ------------------------------------------------------------------

    /// @notice After Final, burn all your YES and NO and receive what they are worth.
    function redeem() external nonReentrant returns (uint256 payout) {
        _expect(Phase.Final);
        uint256 y = yes.balanceOf(msg.sender);
        uint256 n = no.balanceOf(msg.sender);
        payout = payoutFor(y, n);
        if (y > 0) yes.burn(msg.sender, y);
        if (n > 0) no.burn(msg.sender, n);
        if (payout > 0) collateral.safeTransfer(msg.sender, payout);
        emit PaidOut(msg.sender, y, n, payout);
    }

    /// @notice What `y` YES and `n` NO pay out under the final outcome.
    ///         Invalid pays half per token, rounded DOWN, so the market never pays out more than it holds.
    function payoutFor(uint256 y, uint256 n) public view returns (uint256) {
        if (!finalized) return 0;
        if (finalOutcome == Outcome.Yes) return y;
        if (finalOutcome == Outcome.No) return n;
        return (y + n) / 2;
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    function phase() public view returns (Phase) {
        if (finalized) return Phase.Final;
        if (disputer != address(0)) return Phase.Disputed;
        if (proposedAt != 0) return Phase.Proposed;
        if (block.timestamp >= closeTime) return Phase.Closed;
        return Phase.Open;
    }

    /// @notice Collateral the market owes to outcome-token holders right now, assuming
    ///         everyone redeems in the most expensive way available to them.
    function liability() public view returns (uint256) {
        uint256 ys = yes.totalSupply();
        uint256 ns = no.totalSupply();
        if (!finalized) return ys > ns ? ys : ns; // before Final, sets are 1:1 so ys == ns
        if (finalOutcome == Outcome.Yes) return ys;
        if (finalOutcome == Outcome.No) return ns;
        return (ys + ns + 1) / 2; // Invalid: rounded up, the conservative side
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------

    function _finalize(Outcome outcome, bool viaArbiter) internal {
        finalOutcome = outcome;
        finalized = true;
        emit Finalized(outcome, viaArbiter);
    }

    function _expect(Phase p) internal view {
        Phase actual = phase();
        if (actual != p) revert WrongPhase(p, actual);
    }
}

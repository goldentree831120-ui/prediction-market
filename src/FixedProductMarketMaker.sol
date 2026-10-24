// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BinaryMarket} from "./BinaryMarket.sol";

/// @title FixedProductMarketMaker
/// @notice A constant-product pool of YES and NO for one BinaryMarket (the design Gnosis
///         introduced and Polymarket used for its first markets), with liquidity shares as an ERC-20.
///
///   The pool holds y YES and n NO and keeps y * n from falling on any trade.
///   Price of YES = n / (y + n), price of NO = y / (y + n): they always sum to 1, and the
///   YES price reads as the market's probability of YES.
///
///   Buying YES with c collateral: the pool turns c into c YES + c NO (a complete set, via
///   the market), keeps the NO, and pays out enough YES that y * n is back to where it was.
///   Selling YES for c collateral is the reverse: the pool takes YES, redeems c complete
///   sets for c collateral, and charges enough YES that y * n doesn't fall.
///
///   The fee stays in the pool as extra YES and NO, so y * n grows with every trade and the
///   liquidity providers own the growth (no separate fee accounting, like Uniswap v2).
///   Every rounding goes in the pool's favour.
///
///   Trading is allowed only while the market is Open. Liquidity can be withdrawn at any
///   time, as YES and NO tokens, which redeem through the market after resolution.
contract FixedProductMarketMaker is ERC20, ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 public constant BPS = 10_000;
    uint256 public constant MAX_FEE_BPS = 1_000; // 10%
    uint256 public constant MIN_PROB_BPS = 100; // initial odds between 1% and 99%
    /// Shares locked forever by the first provider (as in Uniswap v2). Without this, the first
    /// provider could hold 1 share, donate tokens to inflate its value, and make later providers'
    /// shares round down to nothing (the "first depositor" / inflation attack).
    uint256 public constant MINIMUM_LIQUIDITY = 1000;
    address internal constant LOCKED = address(0xdead);

    BinaryMarket public immutable market;
    IERC20 public immutable collateral;
    IERC20 public immutable yes;
    IERC20 public immutable no;
    uint256 public immutable feeBps;

    event LiquidityAdded(address indexed provider, uint256 collateral, uint256 shares, uint256 yesBack, uint256 noBack);
    event LiquidityRemoved(address indexed provider, uint256 shares, uint256 yesOut, uint256 noOut);
    event Bought(address indexed trader, bool indexed isYes, uint256 collateralIn, uint256 fee, uint256 tokensOut);
    event Sold(address indexed trader, bool indexed isYes, uint256 collateralOut, uint256 fee, uint256 tokensIn);

    error MarketNotOpen();
    error ZeroAmount();
    error BadConfig();
    error BadProbability();
    error NoLiquidity();
    error Slippage(uint256 actual, uint256 limit);
    error InsufficientLiquidity();

    constructor(BinaryMarket market_, uint256 feeBps_) ERC20("Market LP share", "MLP") {
        if (address(market_) == address(0) || feeBps_ > MAX_FEE_BPS) revert BadConfig();
        market = market_;
        collateral = market_.collateral();
        yes = IERC20(address(market_.yes()));
        no = IERC20(address(market_.no()));
        feeBps = feeBps_;
        collateral.forceApprove(address(market_), type(uint256).max);
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    /// Pool balances. Read from the tokens, so a donation simply goes to the liquidity providers.
    function reserves() public view returns (uint256 y, uint256 n) {
        return (yes.balanceOf(address(this)), no.balanceOf(address(this)));
    }

    /// Price of YES (= implied probability) in 1e18 units.
    function priceYes() external view returns (uint256) {
        (uint256 y, uint256 n) = reserves();
        if (y + n == 0) revert NoLiquidity();
        return Math.mulDiv(n, 1e18, y + n);
    }

    /// YES (or NO) tokens received for `collateralIn`, after the fee.
    function quoteBuy(bool isYes, uint256 collateralIn) public view returns (uint256 tokensOut, uint256 fee) {
        (uint256 y, uint256 n) = reserves();
        if (y == 0 || n == 0) revert NoLiquidity();
        (uint256 buyR, uint256 otherR) = isYes ? (y, n) : (n, y);
        fee = Math.mulDiv(collateralIn, feeBps, BPS, Math.Rounding.Ceil);
        uint256 invest = collateralIn - fee;
        // After minting `invest` sets: (buyR + invest) * (otherR + invest), keep the product at buyR * otherR
        // by paying out of the bought side. Round the side that stays in the pool UP.
        uint256 newBuyR = Math.mulDiv(buyR, otherR, otherR + invest, Math.Rounding.Ceil);
        tokensOut = buyR + invest - newBuyR;
    }

    /// YES (or NO) tokens the pool takes to pay out `collateralOut` after the fee.
    function quoteSell(bool isYes, uint256 collateralOut) public view returns (uint256 tokensIn, uint256 fee) {
        (uint256 y, uint256 n) = reserves();
        if (y == 0 || n == 0) revert NoLiquidity();
        (uint256 sellR, uint256 otherR) = isYes ? (y, n) : (n, y);
        // The curve is evaluated as if collateralOut + fee sets were taken out; only collateralOut
        // are actually redeemed, so the fee stays in the pool on both sides.
        uint256 gross = Math.mulDiv(collateralOut, BPS, BPS - feeBps, Math.Rounding.Ceil);
        fee = gross - collateralOut;
        if (gross >= otherR) revert InsufficientLiquidity();
        uint256 newSellR = Math.mulDiv(sellR, otherR, otherR - gross, Math.Rounding.Ceil);
        tokensIn = newSellR + gross - sellR;
    }

    // ------------------------------------------------------------------
    // Liquidity
    // ------------------------------------------------------------------

    /// @notice Add `amount` collateral as liquidity.
    /// @param yesProbBps the starting probability of YES in basis points, used only by the FIRST
    ///        provider (who sets the opening odds); 0 means 50%. Later providers join at the current
    ///        odds: they get back the tokens the pool doesn't need to keep its ratio.
    function addLiquidity(uint256 amount, uint256 yesProbBps, uint256 minShares)
        external
        nonReentrant
        returns (uint256 shares)
    {
        _requireOpen();
        if (amount == 0) revert ZeroAmount();
        uint256 received = _mintSets(amount);
        (uint256 y, uint256 n) = reserves(); // already includes the new sets
        y -= received;
        n -= received;

        uint256 keepY;
        uint256 keepN;
        uint256 supply = totalSupply();
        if (supply == 0) {
            uint256 p = yesProbBps == 0 ? BPS / 2 : yesProbBps;
            if (p < MIN_PROB_BPS || p > BPS - MIN_PROB_BPS) revert BadProbability();
            // price of YES = n / (y + n) = p  =>  keep the full amount of the side that should be
            // scarce in price terms (NO if p >= 50%) and less of the other.
            if (p >= BPS / 2) {
                keepN = received;
                keepY = Math.mulDiv(received, BPS - p, p);
            } else {
                keepY = received;
                keepN = Math.mulDiv(received, p, BPS - p);
            }
            if (received <= MINIMUM_LIQUIDITY) revert ZeroAmount();
            _mint(LOCKED, MINIMUM_LIQUIDITY);
            shares = received - MINIMUM_LIQUIDITY;
        } else {
            // Keep the current ratio: the larger reserve sets the scale. The pool keeps the amounts
            // rounded UP and issues shares rounded DOWN, so joining can't dilute existing shares
            // (the invariant test caught the first version, which rounded the kept amounts down).
            uint256 big = y > n ? y : n;
            keepY = Math.mulDiv(received, y, big, Math.Rounding.Ceil);
            keepN = Math.mulDiv(received, n, big, Math.Rounding.Ceil);
            shares = Math.mulDiv(received, supply, big);
        }
        if (shares == 0) revert ZeroAmount();
        if (shares < minShares) revert Slippage(shares, minShares);

        uint256 yesBack = received - keepY;
        uint256 noBack = received - keepN;
        _mint(msg.sender, shares);
        if (yesBack > 0) yes.safeTransfer(msg.sender, yesBack);
        if (noBack > 0) no.safeTransfer(msg.sender, noBack);
        emit LiquidityAdded(msg.sender, received, shares, yesBack, noBack);
    }

    /// @notice Burn `shares` for the same fraction of the pool's YES and NO. Allowed in every phase.
    function removeLiquidity(uint256 shares, uint256 minYes, uint256 minNo)
        external
        nonReentrant
        returns (uint256 yesOut, uint256 noOut)
    {
        if (shares == 0) revert ZeroAmount();
        (uint256 y, uint256 n) = reserves();
        uint256 supply = totalSupply();
        yesOut = Math.mulDiv(y, shares, supply); // rounded down: dust stays with remaining providers
        noOut = Math.mulDiv(n, shares, supply);
        if (yesOut < minYes) revert Slippage(yesOut, minYes);
        if (noOut < minNo) revert Slippage(noOut, minNo);
        _burn(msg.sender, shares);
        if (yesOut > 0) yes.safeTransfer(msg.sender, yesOut);
        if (noOut > 0) no.safeTransfer(msg.sender, noOut);
        emit LiquidityRemoved(msg.sender, shares, yesOut, noOut);
    }

    // ------------------------------------------------------------------
    // Trading
    // ------------------------------------------------------------------

    function buy(bool isYes, uint256 collateralIn, uint256 minTokensOut)
        external
        nonReentrant
        returns (uint256 tokensOut)
    {
        _requireOpen();
        if (collateralIn == 0) revert ZeroAmount();
        uint256 fee;
        (tokensOut, fee) = quoteBuy(isYes, collateralIn);
        if (tokensOut < minTokensOut) revert Slippage(tokensOut, minTokensOut);
        // All of collateralIn becomes sets in the pool; the fee part stays there on both sides.
        // The quote assumed exactly collateralIn arrives, so fee-on-transfer collateral is refused here.
        uint256 received = _mintSets(collateralIn);
        if (received != collateralIn) revert BadConfig();
        (isYes ? yes : no).safeTransfer(msg.sender, tokensOut);
        emit Bought(msg.sender, isYes, collateralIn, fee, tokensOut);
    }

    function sell(bool isYes, uint256 collateralOut, uint256 maxTokensIn)
        external
        nonReentrant
        returns (uint256 tokensIn)
    {
        _requireOpen();
        if (collateralOut == 0) revert ZeroAmount();
        uint256 fee;
        (tokensIn, fee) = quoteSell(isYes, collateralOut);
        if (tokensIn > maxTokensIn) revert Slippage(tokensIn, maxTokensIn);
        (isYes ? yes : no).safeTransferFrom(msg.sender, address(this), tokensIn);
        market.redeemSets(collateralOut);
        collateral.safeTransfer(msg.sender, collateralOut);
        emit Sold(msg.sender, isYes, collateralOut, fee, tokensIn);
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------

    /// Pulls `amount` collateral and turns what arrived into complete sets held by the pool.
    /// Returns the number of sets minted.
    function _mintSets(uint256 amount) internal returns (uint256 received) {
        uint256 cBefore = collateral.balanceOf(address(this));
        collateral.safeTransferFrom(msg.sender, address(this), amount);
        uint256 arrived = collateral.balanceOf(address(this)) - cBefore;
        uint256 yBefore = yes.balanceOf(address(this));
        market.mintSets(arrived);
        received = yes.balanceOf(address(this)) - yBefore;
    }

    function _requireOpen() internal view {
        if (market.phase() != BinaryMarket.Phase.Open) revert MarketNotOpen();
    }
}

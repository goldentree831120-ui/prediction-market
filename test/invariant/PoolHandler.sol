// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {CommonBase} from "forge-std/Base.sol";
import {StdCheats} from "forge-std/StdCheats.sol";
import {StdUtils} from "forge-std/StdUtils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BinaryMarket} from "../../src/BinaryMarket.sol";
import {FixedProductMarketMaker} from "../../src/FixedProductMarketMaker.sol";
import {MockERC20} from "../utils/MockERC20.sol";

/// Random liquidity, trading and resolution against one pool. After every action it checks
/// that the value of one liquidity share, measured as sqrt(y * n) / shares, did not go down.
contract PoolHandler is CommonBase, StdCheats, StdUtils {
    BinaryMarket public immutable m;
    FixedProductMarketMaker public immutable pool;
    MockERC20 public immutable usd;
    IERC20 public immutable yesT;
    IERC20 public immutable noT;
    address public immutable resolver;
    address[] public users;

    // previous state for the per-share check
    uint256 public lastK;
    uint256 public lastS;
    uint256 public shareValueDrops;
    uint256 public trades;
    uint256 public liquidityOps;
    uint256 public resolved;

    constructor(BinaryMarket m_, FixedProductMarketMaker p, MockERC20 u, address resolver_, address[] memory users_) {
        m = m_;
        pool = p;
        usd = u;
        yesT = IERC20(address(m_.yes()));
        noT = IERC20(address(m_.no()));
        resolver = resolver_;
        users = users_;
    }

    modifier checked() {
        _;
        _checkShareValue();
    }

    function addLiquidity(uint256 who, uint256 amount, uint256 prob) external checked {
        address u = _user(who);
        amount = bound(amount, 1e12, 100_000e18);
        prob = bound(prob, 100, 9900);
        vm.prank(u);
        try pool.addLiquidity(amount, prob, 0) {
            liquidityOps++;
        } catch {}
    }

    function removeLiquidity(uint256 who, uint256 frac) external checked {
        address u = _user(who);
        uint256 bal = pool.balanceOf(u);
        if (bal == 0) return;
        uint256 shares = bound(frac, 1, bal);
        vm.prank(u);
        pool.removeLiquidity(shares, 0, 0);
        liquidityOps++;
    }

    function buy(uint256 who, bool isYes, uint256 amount) external checked {
        address u = _user(who);
        amount = bound(amount, 1, 50_000e18);
        vm.prank(u);
        try pool.buy(isYes, amount, 0) {
            trades++;
        } catch {}
    }

    /// Sell for a random amount of collateral, if the user holds enough tokens for it.
    function sell(uint256 who, bool isYes, uint256 out) external checked {
        address u = _user(who);
        out = bound(out, 1, 20_000e18);
        try pool.quoteSell(isYes, out) returns (uint256 need, uint256) {
            if (need > (isYes ? yesT : noT).balanceOf(u)) return;
            vm.prank(u);
            try pool.sell(isYes, out, need) {
                trades++;
            } catch {}
        } catch {}
    }

    function mintSets(uint256 who, uint256 amount) external checked {
        address u = _user(who);
        amount = bound(amount, 1, 10_000e18);
        vm.prank(u);
        try m.mintSets(amount) {} catch {}
    }

    function warp(uint256 secs) external checked {
        skip(bound(secs, 1, 1 days)); // the market is open 60 days, so most runs trade before it closes
    }

    /// Close and resolve the market, but only after at least 10 trades, so every resolving run
    /// has traded first; the rest of the run then covers liquidity leaving and redemption.
    function closeAndResolve(uint8 outcome) external checked {
        if (trades < 10 || m.finalized()) return;
        if (block.timestamp < m.closeTime()) vm.warp(m.closeTime());
        if (m.phase() != BinaryMarket.Phase.Closed) return;
        vm.prank(resolver);
        m.proposeOutcome(BinaryMarket.Outcome(1 + outcome % 3));
        skip(m.disputeWindow());
        m.finalize();
        resolved++;
    }

    function redeem(uint256 who) external checked {
        if (!m.finalized()) return;
        vm.prank(_user(who));
        m.redeem();
    }

    // ---------------------------------------------------------------------------------------

    function _user(uint256 seed) internal view returns (address) {
        return users[seed % users.length];
    }

    /// sqrt(k) / S must not decrease, i.e. k / S^2 must not decrease. Compared in 1e36 fixed point,
    /// rounding against the check, with 1 unit of tolerance (a 1e-36 relative error).
    function _checkShareValue() internal {
        (uint256 y, uint256 n) = pool.reserves();
        uint256 k = y * n;
        uint256 s = pool.totalSupply();
        if (s != 0 && lastS != 0 && lastK != 0) {
            uint256 kRatio = Math.mulDiv(k, 1e36, lastK);
            uint256 sRatio = Math.mulDiv(s * s, 1e36, lastS * lastS, Math.Rounding.Ceil);
            if (kRatio + 1 < sRatio) shareValueDrops++;
        }
        lastK = k;
        lastS = s;
    }
}

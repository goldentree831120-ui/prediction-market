// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice YES or NO share of one market. Only the market can mint or burn.
///         A plain ERC-20 so it can be traded anywhere (the M2 pool, or a DEX).
contract OutcomeToken is ERC20 {
    address public immutable market;
    uint8 private immutable _decimals;

    error OnlyMarket();

    constructor(string memory name_, string memory symbol_, uint8 decimals_) ERC20(name_, symbol_) {
        market = msg.sender;
        _decimals = decimals_;
    }

    modifier onlyMarket() {
        if (msg.sender != market) revert OnlyMarket();
        _;
    }

    function decimals() public view override returns (uint8) {
        return _decimals;
    }

    function mint(address to, uint256 amount) external onlyMarket {
        _mint(to, amount);
    }

    function burn(address from, uint256 amount) external onlyMarket {
        _burn(from, amount);
    }
}

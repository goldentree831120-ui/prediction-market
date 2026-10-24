#!/usr/bin/env bash
# Plants one bug at a time in the market (M1) or the pool (M2); the test suite must catch each.
# A suite that still passes with a bug planted is not testing that rule.
set -uo pipefail
cd "$(dirname "$0")/.."
FILES=(src/BinaryMarket.sol src/FixedProductMarketMaker.sol)
TMP=$(mktemp -d)
for f in "${FILES[@]}"; do cp "$f" "$TMP/$(basename "$f")"; done
restore() { for f in "${FILES[@]}"; do cp "$TMP/$(basename "$f")" "$f"; done; }
trap 'restore; rm -rf "$TMP" cache/invariant' EXIT

survived=0
mutate() { # name, file, perl substitution
  restore
  perl -0pi -e "$3" "$2"
  if cmp -s "$2" "$TMP/$(basename "$2")"; then echo "?? $1: mutation did not apply"; survived=1; return; fi
  rm -rf cache/invariant # never replay a counterexample saved for an earlier mutant
  forge test >"$TMP/log" 2>&1
  if grep -q "Compiler run failed" "$TMP/log"; then echo "?? $1: mutant does not compile"; survived=1; return; fi
  local inv unit
  # Failing invariants = invariants named in the log minus those marked [PASS]
  # (works for both forge output formats: one test per invariant, or grouped).
  inv=$(comm -23 <(grep -oE 'invariant_[A-Za-z]+' "$TMP/log" | sort -u) \
                 <(grep -E '\[PASS\]' "$TMP/log" | grep -oE 'invariant_[A-Za-z]+' | sort -u) | tr '\n' ' ')
  unit=$(grep -E '^\[FAIL' "$TMP/log" | grep -oE '\] test[A-Za-z_]+' | sort -u | wc -l | tr -d ' ')
  if [ -z "$inv" ] && [ "$unit" = 0 ]; then
    echo "SURVIVED  $1"; survived=1
  else
    echo "caught    $1"
    echo "          invariants: ${inv:-none}   unit/fuzz tests failing: $unit"
  fi
}

M=src/BinaryMarket.sol
P=src/FixedProductMarketMaker.sol

echo "-- market (M1)"
mutate "Invalid pays rounded UP"                 "$M" 's/return \(y \+ n\) \/ 2;/return (y + n + 1) \/ 2;/'
mutate "redeemSets burns only YES"               "$M" 's/        no\.burn\(msg\.sender, amount\);\n        collateral\.safeTransfer\(msg\.sender, amount\);/        collateral.safeTransfer(msg.sender, amount);/'
mutate "mint gives 2 YES per collateral"         "$M" 's/yes\.mint\(msg\.sender, received\);/yes.mint(msg.sender, received * 2);/'
mutate "redeem does not burn losing tokens"      "$M" 's/if \(n > 0\) no\.burn\(msg\.sender, n\);//'
mutate "arbitrate never clears the bond"         "$M" 's/        bondHeld = 0;\n        address to/        address to/'
mutate "minting allowed after close"             "$M" 's/        _expect\(Phase\.Open\);\n        if \(amount == 0\) revert ZeroAmount\(\);\n        \/\/ Pull first/        if (amount == 0) revert ZeroAmount();\n        \/\/ Pull first/'

echo "-- pool (M2)"
mutate "buy charges no fee"                      "$P" 's/fee = Math\.mulDiv\(collateralIn, feeBps, BPS, Math\.Rounding\.Ceil\);/fee = 0;/'
mutate "buy rounds the pool's side down"         "$P" 's/uint256 newBuyR = Math\.mulDiv\(buyR, otherR, otherR \+ invest, Math\.Rounding\.Ceil\);/uint256 newBuyR = Math.mulDiv(buyR, otherR, otherR + invest);/'
mutate "buy pays out 1 wei too much"             "$P" 's/tokensOut = buyR \+ invest - newBuyR;/tokensOut = buyR + invest - newBuyR + 1;/'
mutate "sell ignores the fee in the curve"       "$P" 's/uint256 newSellR = Math\.mulDiv\(sellR, otherR, otherR - gross, Math\.Rounding\.Ceil\);\n        tokensIn = newSellR \+ gross - sellR;/uint256 newSellR = Math.mulDiv(sellR, otherR, otherR - collateralOut, Math.Rounding.Ceil);\n        tokensIn = newSellR + collateralOut - sellR;/'
mutate "joining keeps amounts rounded down"      "$P" 's/keepY = Math\.mulDiv\(received, y, big, Math\.Rounding\.Ceil\);/keepY = Math.mulDiv(received, y, big);/; s/keepN = Math\.mulDiv\(received, n, big, Math\.Rounding\.Ceil\);/keepN = Math.mulDiv(received, n, big);/'
mutate "leaving rounds in the leaver's favour"   "$P" 's/yesOut = Math\.mulDiv\(y, shares, supply\);/yesOut = Math.mulDiv(y, shares, supply, Math.Rounding.Ceil);/; s/noOut = Math\.mulDiv\(n, shares, supply\);/noOut = Math.mulDiv(n, shares, supply, Math.Rounding.Ceil);/'
mutate "trading allowed after close"             "$P" 's/        _requireOpen\(\);\n        if \(collateralIn == 0\)/        if (collateralIn == 0)/'
mutate "no locked minimum liquidity"             "$P" 's/            _mint\(LOCKED, MINIMUM_LIQUIDITY\);\n            shares = received - MINIMUM_LIQUIDITY;/            shares = received;/'
mutate "price of YES uses the YES reserve"       "$P" 's/return Math\.mulDiv\(n, 1e18, y \+ n\);/return Math.mulDiv(y, 1e18, y + n);/'

exit $survived

"""What the book pays to get out of a position right now.

The site marks a position at the midpoint of the book, and so did I on the morning of
4 Oct (memo section 20): the midpoint said the smoke-run position was down $0.15, the
bids said $2.45, and a little later they said $4.63. Nothing about an exit may be an
estimate again: this module walks the resting bids, best first, charges the taker fee
on the way out, and returns the dollars a sale actually brings. `lp` prints it for
every planned quote (the `exit $` column, filtered by --max-exit), writes it on every
fill, and `lp --positions` prints it for whatever the account holds."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .fees import DEFAULT_POLYMARKET_RATE, polymarket_taker_fee

Level = tuple[float, float]  # (price, shares)


@dataclass(frozen=True)
class Unwind:
    shares: float  # asked for
    filled: float  # what the bids absorb
    gross: float  # dollars before the fee
    fee: float

    @property
    def complete(self) -> bool:
        return self.filled >= self.shares - 1e-9

    @property
    def net(self) -> float:
        return self.gross - self.fee

    @property
    def avg_price(self) -> float | None:
        return self.gross / self.filled if self.filled > 0 else None


def unwind_into(bids: Iterable[Level], shares: float, *, fee_rate: float = DEFAULT_POLYMARKET_RATE) -> Unwind:
    """Sell `shares` into `bids` (price, size), best price first. A NO position sells into the
    NO bids, which in the YES book are the asks at 1 - price; the caller passes those."""
    left = max(shares, 0.0)
    gross = fee = 0.0
    for price, size in sorted(bids, key=lambda lv: -lv[0]):
        if left <= 1e-9:
            break
        if price <= 0 or size <= 0:
            continue
        take = min(size, left)
        gross += take * price
        fee += polymarket_taker_fee(price, take, fee_rate)
        left -= take
    return Unwind(shares, shares - left, gross, fee)


def exit_cost(entry_price: float, shares: float, bids: Iterable[Level], *, fee_rate: float = DEFAULT_POLYMARKET_RATE) -> float | None:
    """Dollars lost by buying `shares` at `entry_price` and selling them into `bids` at once,
    fee included. None when the bids cannot absorb them: there is no exit at any price."""
    u = unwind_into(bids, shares, fee_rate=fee_rate)
    if not u.complete:
        return None
    return round(entry_price * shares - u.net, 4)


def no_bids_from_yes_asks(asks: Iterable[Level]) -> list[Level]:
    return [(round(1.0 - price, 4), size) for price, size in asks]

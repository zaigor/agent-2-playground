"""pm_scanner: read-only opportunity scanner for Polymarket and Kalshi.

Finds (a) multi-outcome "dutch book" arbitrage inside Polymarket negative-risk
events, (b) cross-platform price gaps between Polymarket and Kalshi on the same
question, and (c) near-certain outcomes trading below $1 close to resolution.
Every edge is reported *net of taker fees* and sized against a cash budget.
"""

__version__ = "0.1.0"

"""Human-readable output."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from .models import Opportunity
from .odds import price_to_decimal


def _leg(l) -> str:
    what = f" {l.label}" if l.label else ""
    return f"long {l.team}: {l.venue}{what} @ {l.best:.3f}"


def format_opps(opps: List[Opportunity], limit: int = 25) -> str:
    if not opps:
        return "No arbitrage found at current prices (after fees)."
    now = datetime.now(timezone.utc)
    lines = []
    for i, o in enumerate(opps[:limit], 1):
        ann = o.annualized(now)
        start = o.start.strftime("%b %d %H:%MZ") if o.start else "?"
        lines.append(
            f"#{i} [{o.game}] {o.title}  (start {start})\n"
            f"    leg A: {_leg(o.leg_a)}   (≈ decimal {price_to_decimal(o.leg_a.best):.2f})\n"
            f"    leg B: {_leg(o.leg_b)}   (≈ decimal {price_to_decimal(o.leg_b.best):.2f})\n"
            f"    top-of-book edge {o.top_edge*100:.2f}% | size {o.contracts:.0f} | cost ${o.cost:.2f} "
            f"(fees ${o.fees:.2f}) | profit ${o.profit:.2f} | ROI {o.roi*100:.2f}%"
            + (f" | ann. {ann*100:.0f}%" if ann is not None else "")
            + f"\n    if cancelled: est. P&L ${o.void_pnl:+.2f}"
        )
        for w in o.warnings:
            lines.append(f"    ! {w}")
    return "\n".join(lines)


def opp_to_dict(o: Opportunity) -> dict:
    leg = lambda l: {"venue": l.venue, "side": l.side, "team": l.team, "instrument": l.instrument,
                     "label": l.label, "best_ask": l.best, "fee_model": l.fee_model}
    return {"game": o.game, "match": o.title, "start": o.start.isoformat() if o.start else None,
            "leg_a": leg(o.leg_a), "leg_b": leg(o.leg_b), "contracts": o.contracts,
            "cost": round(o.cost, 4), "fees": round(o.fees, 4), "profit": round(o.profit, 4),
            "roi": round(o.roi, 6), "void_pnl": round(o.void_pnl, 4), "top_edge": round(o.top_edge, 6), "warnings": o.warnings}

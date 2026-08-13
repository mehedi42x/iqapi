"""Billing layer - raw ``internal-billing.get-balances`` data.

Deliberately separate from the trading account layer: billing knows about
*every* balance the user owns (real, practice, tournament, promo, internal),
while ``account`` only cares about the tradable one currently selected.
"""

from .billing import BillingManager

__all__ = ["BillingManager"]

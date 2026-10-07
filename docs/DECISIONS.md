# Architecture decisions

## One virtual account per user, vault selected by reference metadata

A per-vault DVA would make routing deposits simple but consumes provider-issued accounts quickly. Paystack businesses have a 1,000 DVA allocation cap. At two or three vaults per customer, a per-vault design reaches that hard ceiling at roughly 300–500 customers. The demo therefore models one customer account and routes each confirmed charge using signed event metadata/reference mapping. Production must persist a server-created pending deposit reference and never trust arbitrary client-supplied vault metadata.

## Ledger rows are append-only credits

A balance is a convenient projection; the ledger entry is the auditable event. The unique provider reference is enforced by Postgres, since process-local checks cannot prevent simultaneous webhook workers from double-crediting.

## Provider calls happen outside transactions

Database locks protect state transitions, not HTTP duration. A committed payout claim releases pool resources and gives concurrent workers a clear rejection state. A separate reconciliation process should recover long-lived `payout_initiated` rows after provider timeouts.

## Mock first, real adapter explicit

The default provider has deterministic local DVA data and success/failure transfers. `PAYSTACK_MODE=real` selects the HTTP adapter for customer/DVA provisioning. To keep the sample narrow, payouts use one operator-configured `PAYSTACK_RECIPIENT_CODE`; this is not a safe multi-user payout configuration and is not production-ready. No live credentials or recipient are included.


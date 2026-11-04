# Ledger

Fixed in commit `ae5a684` and `ad680750c1b2`.

Statuses: UPDATE, SIGTERM, USDC, USDT, VERIFIED, PASS, FIFO, MERGED, BLOCKED (key = status).
Matching uses FIFO order; token ledger in USDC=1.

Rows carry a `ledger_row_kind` field, see `token()`.

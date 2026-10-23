def summarize(rows):
    """Return signed totals for each account in deterministic order.

    Rows with the same key represent retries. An exact retry is counted once;
    reusing a key for a different account or amount is invalid.
    """
    seen = {}
    totals = {}

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each row must be a dictionary")

        try:
            key = row["key"]
            account = row["account"]
            amount = row["amount"]
        except KeyError as exc:
            raise ValueError("each row must contain key, account, and amount") from exc

        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a nonempty string")
        if not isinstance(account, str) or not account.strip():
            raise ValueError("account must be a nonempty string")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
            raise ValueError("amount must be a nonzero integer")

        entry = (account, amount)
        if key in seen:
            if seen[key] != entry:
                raise ValueError("key was already used for a different row")
            continue

        seen[key] = entry
        totals[account] = totals.get(account, 0) + amount

    return {account: totals[account] for account in sorted(totals)}

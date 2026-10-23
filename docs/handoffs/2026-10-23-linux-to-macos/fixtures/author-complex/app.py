"""Exact reconciliation of a three-column CSV ledger."""

import re


_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]{1,2})?\Z")


def _csv_rows(text):
    """Read CSV records, rejecting quotes outside their permitted positions."""
    fields = []
    field = []
    state = "start"
    index = 0

    while index < len(text):
        char = text[index]

        if state == "quoted":
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    field.append('"')
                    index += 2
                    continue
                state = "closed"
            else:
                field.append(char)
            index += 1
            continue

        if char == ",":
            fields.append("".join(field))
            field = []
            state = "start"
        elif char == "\r" or char == "\n":
            if fields or state != "start":
                fields.append("".join(field))
                yield fields
            else:
                yield []
            fields = []
            field = []
            state = "start"
            if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
        elif state == "start":
            if char == '"':
                state = "quoted"
            else:
                field.append(char)
                state = "unquoted"
        elif state == "unquoted":
            if char == '"':
                raise ValueError("Quote in unquoted CSV field")
            field.append(char)
        else:
            raise ValueError("Text after closing CSV quote")

        index += 1

    if state == "quoted":
        raise ValueError("Unterminated CSV quote")
    if fields or state != "start":
        fields.append("".join(field))
        yield fields


def _integer_from_ascii(digits):
    """Avoid Python's limit on decimal string to integer conversions."""
    value = 0
    for digit in digits:
        value = value * 10 + ord(digit) - ord("0")
    return value


def _cents(amount):
    if _AMOUNT.fullmatch(amount) is None:
        raise ValueError("Invalid amount")

    negative = amount.startswith("-")
    if negative:
        amount = amount[1:]
    whole, separator, fraction = amount.partition(".")
    cents = _integer_from_ascii(whole) * 100
    if separator:
        cents += _integer_from_ascii(fraction.ljust(2, "0"))
    return -cents if negative else cents


def reconcile(csv_text):
    """Return unique posted IDs and account balances in exact integer cents."""
    if not isinstance(csv_text, str):
        raise ValueError("CSV input must be text")
    if csv_text.startswith("\ufeff"):
        csv_text = csv_text[1:]

    rows = iter(_csv_rows(csv_text))
    if next(rows, None) != ["id", "account", "amount"]:
        raise ValueError("Invalid CSV header")

    seen = {}
    balances = {}
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            raise ValueError("CSV row must have three fields")

        record_id, account, amount = (field.strip() for field in row)
        if not record_id or not account:
            raise ValueError("ID and account must be nonempty")
        cents = _cents(amount)
        record = (account, cents)
        if record_id in seen:
            if seen[record_id] != record:
                raise ValueError("Conflicting duplicate ID")
            continue

        seen[record_id] = record
        balances[account] = balances.get(account, 0) + cents

    return {"posted": len(seen), "balances": dict(sorted(balances.items()))}

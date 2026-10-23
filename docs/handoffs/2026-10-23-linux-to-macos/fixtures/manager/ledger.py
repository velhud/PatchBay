"""A small persistent, replay-safe money ledger backed by SQLite."""

import sqlite3
import threading


def _validate_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _validate_entry(key, account, amount):
    _validate_text(key, "key")
    _validate_text(account, "account")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
        raise ValueError("amount must be a nonzero integer")


def _encode_amount(amount):
    """Encode a nonzero Python integer without decimal conversion."""
    magnitude = abs(amount)
    size = max(1, (magnitude.bit_length() + 7) // 8)
    return (b"\x01" if amount < 0 else b"\x00") + magnitude.to_bytes(size, "big")


def _decode_legacy_decimal(value):
    """Read rows written by the earlier TEXT codec in bounded chunks."""
    if not value or value in ("+", "-"):
        raise ValueError("invalid stored amount")
    negative = value[0] == "-"
    digits = value[1:] if value[0] in "+-" else value
    if not digits or not digits.isascii() or not digits.isdigit():
        raise ValueError("invalid stored amount")
    result = 0
    first_width = len(digits) % 9 or 9
    result = int(digits[:first_width])
    for offset in range(first_width, len(digits), 9):
        result = result * 1_000_000_000 + int(digits[offset:offset + 9])
    return -result if negative else result


def _decode_amount(value):
    """Decode a BLOB amount, with bounded compatibility for legacy TEXT."""
    if isinstance(value, str):
        return _decode_legacy_decimal(value)
    if not isinstance(value, bytes) or len(value) < 2 or value[0] not in (0, 1):
        raise ValueError("invalid stored amount")
    magnitude = int.from_bytes(value[1:], "big")
    if magnitude == 0:
        raise ValueError("invalid stored amount")
    return -magnitude if value[0] else magnitude


class Ledger:
    """A persistent ledger whose entry keys are idempotency keys."""

    def __init__(self, path):
        self._connection = sqlite3.connect(path, timeout=30)
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._lock = threading.RLock()
        self._closed = False
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS ledger_entries ("
            "key TEXT PRIMARY KEY, account TEXT NOT NULL, amount BLOB NOT NULL)"
        )
        self._connection.commit()

    def post(self, key, account, amount):
        """Insert an entry, or return False for an identical replay."""
        _validate_entry(key, account, amount)
        encoded_amount = _encode_amount(amount)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT account, amount FROM ledger_entries WHERE key = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != account or _decode_amount(existing[1]) != amount:
                        raise ValueError("key already exists with different entry")
                    self._connection.commit()
                    return False

                self._connection.execute(
                    "INSERT INTO ledger_entries (key, account, amount) VALUES (?, ?, ?)",
                    (key, account, encoded_amount),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def balance(self, account):
        """Return the signed total for an account, or zero if it is absent."""
        _validate_text(account, "account")
        with self._lock:
            values = self._connection.execute(
                "SELECT amount FROM ledger_entries WHERE account = ?", (account,)
            )
            return sum(_decode_amount(row[0]) for row in values)

    def rows(self):
        """Return copied ledger rows sorted by key."""
        with self._lock:
            values = self._connection.execute(
                "SELECT key, account, amount FROM ledger_entries ORDER BY key"
            )
            return [
                {"key": key, "account": account, "amount": _decode_amount(amount)}
                for key, account, amount in values
            ]

    def close(self):
        """Close the underlying SQLite connection."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

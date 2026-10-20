"""Stable, structured failures shared by every author transport."""
from __future__ import annotations


class AuthorError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409, **details):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


def require(condition, code: str, message: str, *, status: int = 409, **details) -> None:
    if not condition:
        raise AuthorError(code, message, status=status, **details)

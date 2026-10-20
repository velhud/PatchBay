"""Native ChatGPT file inputs are transport only; bytes enter the ordinary validator."""
from __future__ import annotations

import fnmatch
import ipaddress
import os
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..errors import AuthorError, require


class NativeDownloader:
    def __init__(self, store):
        self.store = store

    def validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        require(parsed.scheme == "https" and parsed.hostname and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None and not parsed.fragment,
                "INVALID_FILE_REFERENCE", "Native files need a legitimate HTTPS download URL")
        host = parsed.hostname.lower()
        require(any(fnmatch.fnmatchcase(host, pattern.lower()) for pattern in self.store.limits.native_download_hosts),
                "INVALID_FILE_REFERENCE", "Download hostname is not approved for native file transport")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise AuthorError("FILE_UNAVAILABLE", "Native file hostname could not be resolved; use a fresh file reference or manual upload") from exc
        require(addresses and all(ipaddress.ip_address(address).is_global for address in addresses),
                "INVALID_FILE_REFERENCE", "Native downloads cannot target local or private network addresses")

    def download(self, artifact_file: dict) -> Path:
        require(isinstance(artifact_file, dict) and set(artifact_file) <= {"download_url", "file_id", "mime_type", "file_name"},
                "INVALID_FILE_REFERENCE", "Unexpected native file fields")
        url, file_id = artifact_file.get("download_url"), artifact_file.get("file_id")
        require(isinstance(url, str) and len(url) <= 32768 and isinstance(file_id, str) and 1 <= len(file_id) <= 500,
                "INVALID_FILE_REFERENCE", "download_url and file_id are required")
        self.validate_url(url)
        owner = self
        class Redirect(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                owner.validate_url(newurl)
                return super().redirect_request(request, fp, code, msg, headers, newurl)
        descriptor, name = tempfile.mkstemp(prefix="native-", suffix=".zip", dir=self.store.home / "uploads")
        path = Path(name)
        started, size = time.monotonic(), 0
        try:
            with os.fdopen(descriptor, "wb") as target:
                opener = build_opener(Redirect())
                with opener.open(Request(url, headers={"User-Agent": "PatchBay-Author/0.1"}), timeout=10) as source:
                    self.validate_url(source.geturl())
                    while True:
                        require(time.monotonic() - started <= 45, "FILE_UNAVAILABLE", "Native transfer exceeded its bounded transfer budget; use a fresh reference or manual upload")
                        # read1 returns available bytes rather than waiting to
                        # fill 64 KiB while a slow stream evades the total check.
                        chunk = (getattr(source, "read1", None) or source.read)(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        require(size <= self.store.limits.archive.compressed_bytes, "RESOURCE_LIMIT", "Native package exceeds its byte budget")
                        target.write(chunk)
                target.flush(); os.fsync(target.fileno())
            require(size > 0, "FILE_UNAVAILABLE", "Native file was empty")
            return path
        except BaseException as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, AuthorError):
                raise
            raise AuthorError("FILE_UNAVAILABLE", "Native file could not be retrieved; provide a fresh supported reference or upload the package manually") from exc

"""Bounded archive ingestion into content storage; no archive writes into source."""
from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from pydantic import ValidationError

from ..errors import AuthorError, require
from ..util import fingerprint, safe_path, strict_json
from .schema import Manifest


@dataclass(frozen=True)
class ImportedPackage:
    manifest: Manifest
    archive_sha: str
    payloads: dict[str, str]
    fingerprint: str


class PackageReader:
    def __init__(self, store):
        self.store = store

    def read(self, path: Path) -> ImportedPackage:
        limits = self.store.limits.archive
        archive_sha = self.store.blobs.put_file(Path(path), limit=limits.compressed_bytes)
        try:
            with zipfile.ZipFile(self.store.blobs.path(archive_sha)) as archive:
                infos = archive.infolist()
                require(0 < len(infos) <= limits.members, "INVALID_PACKAGE", "Archive member count exceeds its contract")
                names, folded, regular, expanded = set(), set(), {}, 0
                for info in infos:
                    require(info.orig_filename == info.filename and "\x00" not in info.orig_filename,
                            "INVALID_PACKAGE", "Ambiguous archive member name")
                    name = info.filename[:-1] if info.is_dir() else info.filename
                    safe_path(name)
                    require(name not in names and name.casefold() not in folded, "INVALID_PACKAGE", "Duplicate or case-colliding archive member", path=name)
                    names.add(name); folded.add(name.casefold())
                    kind = stat.S_IFMT(info.external_attr >> 16)
                    require(kind in ({0, stat.S_IFDIR} if info.is_dir() else {0, stat.S_IFREG}),
                            "INVALID_PACKAGE", "Links and special archive entries are unsupported", path=name)
                    require(not info.flag_bits & 1 and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                            "INVALID_PACKAGE", "Unsupported archive encryption or compression")
                    require(info.file_size >= 0 and info.file_size <= limits.member_bytes, "RESOURCE_LIMIT", "Archive member is too large", path=name)
                    expanded += info.file_size
                    require(expanded <= limits.expanded_bytes, "RESOURCE_LIMIT", "Archive expands beyond its byte budget")
                    if info.is_dir():
                        require(info.file_size == 0, "INVALID_PACKAGE", "Directory entries cannot contain bytes")
                    else:
                        regular[name] = info
                require("manifest.json" in regular, "INVALID_PACKAGE", "Archive is missing manifest.json")
                require(regular["manifest.json"].file_size <= limits.manifest_bytes, "RESOURCE_LIMIT", "Manifest is too large")
                manifest = Manifest.model_validate(strict_json(archive.read(regular["manifest.json"])))
                declared = {operation.payload for operation in manifest.operations if operation.payload is not None}
                require(set(regular) == declared | ({"manifest.json", "REVIEW.md", "changes.diff"} & set(regular)),
                        "INVALID_PACKAGE", "Archive has missing or undeclared payloads")
                require(declared <= set(regular), "INVALID_PACKAGE", "Declared payload is absent")
                for name in regular:
                    parts = name.split("/")
                    require(not any("/".join(parts[:i]) in regular for i in range(1, len(parts))),
                            "INVALID_PACKAGE", "Archive contains a file/directory collision")
                payloads = {}
                for name in sorted(declared):
                    with archive.open(regular[name]) as member:
                        sha = self.store.blobs.put_stream(member, limit=min(regular[name].file_size, limits.member_bytes))
                    require(self.store.blobs.path(sha).stat().st_size == regular[name].file_size,
                            "INVALID_PACKAGE", "Archive member size mismatch")
                    payloads[name] = sha
                for operation in manifest.operations:
                    if operation.payload is not None:
                        require(payloads[operation.payload] == operation.after_sha256,
                                "INVALID_PACKAGE", "Payload bytes do not match the manifest hash", path=operation.path)
                return ImportedPackage(manifest, archive_sha, payloads, fingerprint(manifest.semantics()))
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValidationError) as exc:
            raise AuthorError("INVALID_PACKAGE", "Archive or manifest failed validation", detail=str(exc)[:2000]) from exc

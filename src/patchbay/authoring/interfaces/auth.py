"""Operator, author, and reader authority; MCP writes require recorded client qualification."""
from __future__ import annotations

import hmac
import uuid
import hashlib
import secrets
from contextlib import contextmanager
from ..errors import require
from ..persistence.gates import mutation
from ..persistence.locks import file_lock
from ..util import canonical, strict_json, utc_now, fingerprint, safe_id
from .catalog import identity as catalog_identity, OUTPUT_PROFILE, OUTPUT_PROFILES
from ..authority import AuthIdentity, assert_operator as require_operator, assert_task_scope, current_identity, identity_context
import json


class Access:
    def __init__(self, store):
        self.store = store

    def authenticate(self, authorization: str | None) -> AuthIdentity:
        require(isinstance(authorization, str) and authorization.startswith("Bearer ") and len(authorization) <= 1024,
                "UNAUTHORIZED", "Author runtime authorization is required", status=401)
        candidate = authorization[7:]
        tokens = strict_json((self.store.home / "tokens.json").read_bytes())
        for role in ("operator", "author", "reader"):
            if isinstance(tokens.get(role), str) and hmac.compare_digest(tokens[role], candidate):
                # The three original role tokens remain a shared compatibility
                # principal. New tokens below carry a stable per-principal ID.
                return AuthIdentity("local-operator", role, None, legacy=True)
        token_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        row = self.store.one("SELECT value FROM metadata WHERE key=?", ("author.principal.token." + token_digest,), required=False)
        if row is not None:
            value = strict_json(row["value"])
            role, principal_id, task_ids = value.get("role"), value.get("principal_id"), value.get("task_ids")
            require(role in {"operator", "author", "reader"} and isinstance(principal_id, str) and principal_id,
                    "CORRUPT_STATE", "Stored principal credential is invalid")
            require(task_ids is None or isinstance(task_ids, list) and all(isinstance(item, str) for item in task_ids),
                    "CORRUPT_STATE", "Stored principal task scope is invalid")
            require(value.get("revoked") is not True, "UNAUTHORIZED", "Author runtime authorization was revoked", status=401)
            return AuthIdentity(principal_id, role, tuple(task_ids) if task_ids is not None else None)
        require(False, "UNAUTHORIZED", "Author runtime authorization was not accepted", status=401)

    def role(self, authorization: str | None) -> str:
        """Compatibility role-only view; transports should bind authenticate()."""
        return self.authenticate(authorization).role

    @contextmanager
    def as_identity(self, identity: AuthIdentity):
        """Bind a previously authenticated identity for one request boundary."""
        with identity_context(identity):
            yield identity

    def assert_task(self, task_id: str, identity: AuthIdentity | None = None) -> None:
        assert_task_scope(task_id, identity)

    def assert_global_operator(self, identity: AuthIdentity | None = None) -> None:
        require_operator(identity=identity)

    def assert_operator(self, task_id: str | None = None, identity: AuthIdentity | None = None) -> None:
        require_operator(task_id, identity)

    @mutation
    def issue(self, principal_id: str, role: str, task_ids: list[str] | tuple[str, ...] | None = None,
              *, key: str) -> dict:
        """Issue a durable principal token; the bearer is returned only here.

        A copy is held in the private Author database solely so a lost response
        can be replayed with the same idempotency key. It never enters the
        public operation result, payload, event stream, or status projection.
        """
        self.assert_global_operator()
        safe_id(principal_id)
        require(principal_id not in {"local-operator", "legacy-role-token-users"}, "INVALID_REQUEST",
                "Principal ID is reserved for local compatibility identities")
        require(role in {"operator", "author", "reader"}, "INVALID_REQUEST", "Unknown runtime role")
        if task_ids is not None:
            require(isinstance(task_ids, (list, tuple)) and 0 < len(task_ids) <= 1000,
                    "INVALID_REQUEST", "Task scope must contain a bounded non-empty list")
            task_ids = tuple(sorted(set(safe_id(task_id) for task_id in task_ids)))
        scope = list(task_ids) if task_ids is not None else None
        semantics = {"principal_id": principal_id, "role": role, "task_ids": scope}
        op, _ = self.store.begin_operation(kind="principal.issue", key=key,
                    fingerprint=fingerprint(semantics), payload=semantics)
        private_key = "author.principal.issue." + op["id"]
        credential_id = "credential_" + op["id"][3:]
        receipt = {**semantics, "credential_id": credential_id, "request_operation_id": op["id"]}
        if op["state"] == "completed":
            private = self.store.one("SELECT value FROM metadata WHERE key=?", (private_key,), required=False)
            require(private is not None, "CORRUPT_STATE", "Issued credential replay material is unavailable")
            saved = strict_json(private["value"])
            require(saved.get("principal_id") == principal_id and saved.get("role") == role
                    and saved.get("task_ids") == scope and saved.get("credential_id") == credential_id
                    and isinstance(saved.get("token"), str),
                    "CORRUPT_STATE", "Issued credential replay record does not match its operation")
            return {**receipt, "token": saved["token"]}

        require(op["state"] == "queued", "OPERATION_CONFLICT", "Credential issue operation is not retryable")
        with self.store.transaction() as conn:
            private = conn.execute("SELECT value FROM metadata WHERE key=?", (private_key,)).fetchone()
            if private:
                saved = strict_json(private[0])
                token = saved.get("token")
                require(saved.get("principal_id") == principal_id and saved.get("role") == role
                        and saved.get("task_ids") == scope and saved.get("credential_id") == credential_id
                        and isinstance(token, str),
                        "CORRUPT_STATE", "Issued credential replay record does not match its operation")
            else:
                token = secrets.token_urlsafe(48)
                saved = {**semantics, "credential_id": credential_id, "token": token}
                conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (private_key, canonical(saved).decode()))
            token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            token_key = "author.principal.token." + token_digest
            identity_record = canonical({**semantics, "credential_id": credential_id, "revoked": False}).decode()
            existing = conn.execute("SELECT value FROM metadata WHERE key=?", (token_key,)).fetchone()
            require(existing is None or existing[0] == identity_record, "CORRUPT_STATE", "Credential digest collision")
            conn.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (token_key, identity_record))
            now = utc_now()
            conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                         (canonical(receipt).decode(), now, op["id"]))
            self.store.event(conn, "principal.credential_issued", "", {"operation_id": op["id"], "principal_id": principal_id, "role": role})
        return {**receipt, "token": token}

    @mutation
    def revoke(self, credential_id: str, *, key: str) -> dict:
        """Revoke one issued token without changing its principal or leases."""
        self.assert_global_operator()
        safe_id(credential_id)
        require(credential_id.startswith("credential_"), "INVALID_REQUEST", "Credential ID is invalid")
        operation_id = "op_" + credential_id[len("credential_"):]
        op, _ = self.store.begin_operation(kind="principal.revoke", key=key,
                    fingerprint=fingerprint({"credential_id": credential_id}),
                    payload={"credential_id": credential_id})
        if op["state"] == "completed":
            return json.loads(op["result"])
        require(op["state"] == "queued", "OPERATION_CONFLICT", "Credential revocation is not retryable")
        private_key = "author.principal.issue." + operation_id
        with self.store.transaction() as conn:
            private = conn.execute("SELECT value FROM metadata WHERE key=?", (private_key,)).fetchone()
            require(private is not None, "NOT_FOUND", "Credential is unavailable", status=404)
            saved = strict_json(private[0])
            require(saved.get("credential_id") == credential_id and isinstance(saved.get("token"), str),
                    "CORRUPT_STATE", "Credential private record is invalid")
            token_digest = hashlib.sha256(saved["token"].encode("utf-8")).hexdigest()
            token_key = "author.principal.token." + token_digest
            mapping = conn.execute("SELECT value FROM metadata WHERE key=?", (token_key,)).fetchone()
            require(mapping is not None, "CORRUPT_STATE", "Credential authentication mapping is missing")
            value = strict_json(mapping[0])
            value["revoked"] = True
            conn.execute("UPDATE metadata SET value=? WHERE key=?", (canonical(value).decode(), token_key))
            receipt = {"credential_id": credential_id, "principal_id": saved["principal_id"],
                       "revoked": True, "request_operation_id": op["id"]}
            now = utc_now()
            conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                         (canonical(receipt).decode(), now, op["id"]))
            self.store.event(conn, "principal.credential_revoked", "", {"operation_id": op["id"],
                             "credential_id": credential_id, "principal_id": saved["principal_id"]})
        return receipt

    def require_mutation(self, role: str, *, operator_only: bool = False, mcp: bool = False, output_profile: str | None = None) -> None:
        require(role in ({"operator"} if operator_only else {"operator", "author"}),
                "FORBIDDEN", "This action exceeds the current runtime role", status=403)
        from .catalog import assert_current_release
        assert_current_release()
        if mcp:
            require(self.qualified(role, output_profile=output_profile) is not None, "QUALIFICATION_REQUIRED",
                    "Live MCP writes are not qualified for this runtime; use the artifact import workflow", status=403)

    _QUALIFICATION_REGISTRY = "author.client.qualifications.v1"
    _QUALIFICATION_PREFIX = "author.client.qualification."

    def _qualification_key(self, record):
        import math
        require(isinstance(record, dict) and isinstance(record.get('binding'), dict)
                and isinstance(record.get('evidence'), dict), "CORRUPT_STATE", "Invalid client qualification record")
        evidence, binding = record['evidence'], record['binding']
        require(evidence.get('role') in {'operator', 'author'} and binding.get('output_profile') in OUTPUT_PROFILES
                and isinstance(record.get('instance_id'), str)
                and isinstance(record.get('qualification_id'), str)
                and record['qualification_id'].startswith('qualification_')
                and isinstance(record.get('created'), (float, int)) and type(record.get('created')) is not bool
                and isinstance(record.get('expires'), (float, int)) and type(record.get('expires')) is not bool
                and math.isfinite(record['created']) and math.isfinite(record['expires'])
                and record['expires'] > record['created']
                and evidence.get('ordinary_chat') is True and evidence.get('write_supported') is True,
                "CORRUPT_STATE", "Invalid client qualification binding")
        require(all(isinstance(evidence.get(name), str) and 0 < len(evidence[name]) <= 4000
                    for name in ('model', 'client', 'probe_description', 'observed_at',
                                 'account_label', 'mode', 'protocol', 'ingress', 'plugin_version'))
                and evidence['mode'] == 'ordinary_chat'
                and evidence.get('output_profile', OUTPUT_PROFILE) == binding['output_profile']
                and isinstance(record.get('actor'), str) and 0 < len(record['actor']) <= 200,
                'CORRUPT_STATE', 'Invalid client qualification observation')
        principal = evidence.get('principal_id', 'local-operator')
        require(isinstance(principal, str) and principal, "CORRUPT_STATE", "Invalid qualification principal")
        return self._QUALIFICATION_PREFIX + fingerprint({'instance_id': record['instance_id'],
            'binding': binding, 'role': evidence['role'], 'principal_id': principal})

    def _legacy_qualification(self):
        path = self.store.home / "client-qualification.json"
        if not path.is_file() or path.is_symlink():
            return None
        record = strict_json(path.read_bytes())
        self._qualification_key(record)
        return record

    def qualified(self, role: str | None = None, *, output_profile: str | None = None) -> dict | None:
        from .catalog import STARTUP_SOURCE_SHA256, observed_source_digest
        if observed_source_digest() != STARTUP_SOURCE_SHA256:
            return None
        identity = current_identity()
        marker = self.store.one("SELECT value FROM metadata WHERE key=?", (self._QUALIFICATION_REGISTRY,), required=False)
        if marker is not None:
            require(strict_json(marker['value']) == {'version': 1}, "CORRUPT_STATE", "Invalid qualification registry")
        legacy = self._legacy_qualification() if marker is None else None
        for profile in ([output_profile] if output_profile is not None else OUTPUT_PROFILES):
            if profile not in OUTPUT_PROFILES:
                continue
            binding = catalog_identity(profile)
            # Reader capabilities may inspect this principal's own observation;
            # write eligibility still requires an explicit author/operator role.
            roles = [role] if role in {'operator', 'author'} else ('operator', 'author') if role in {None, 'reader'} else ()
            for candidate_role in roles:
                key = self._QUALIFICATION_PREFIX + fingerprint({'instance_id': self.store.identity['instance_id'],
                    'binding': binding, 'role': candidate_role, 'principal_id': identity.principal_id})
                row = self.store.one("SELECT value FROM metadata WHERE key=?", (key,), required=False) if marker else None
                record = strict_json(row['value']) if row else legacy
                if record is None:
                    continue
                actual_key = self._qualification_key(record)
                require(row is None or actual_key == key, "CORRUPT_STATE", "Qualification registry key mismatch")
                if (actual_key == key and record['expires'] > utc_now()):
                    return record
        return None

    @mutation
    def qualify(self, evidence: dict, *, actor: str, key: str | None = None) -> dict:
        self.assert_global_operator()
        from .catalog import assert_current_release
        assert_current_release()
        allowed = {"model", "effort", "client", "client_version", "account_label", "mode", "role", "protocol", "ingress",
                   "plugin_version", "ordinary_chat", "write_supported", "probe_description", "observed_at", "output_profile"}
        allowed.add("principal_id")
        require(isinstance(evidence, dict) and set(evidence) <= allowed
                and evidence.get("ordinary_chat") is True and evidence.get("write_supported") is True,
                "INVALID_QUALIFICATION", "Qualification must describe an observed ordinary-Chat write trial")
        for name in ("model", "client", "probe_description", "observed_at"):
            require(isinstance(evidence.get(name), str) and 0 < len(evidence[name]) <= 4000,
                    "INVALID_QUALIFICATION", "Qualification is missing an explicit observation", field=name)
        require(isinstance(actor, str) and 0 < len(actor) <= 200, "INVALID_QUALIFICATION", "Operator identity is required")
        for name in ("account_label", "mode", "role", "protocol", "ingress", "plugin_version"):
            require(isinstance(evidence.get(name), str) and 0 < len(evidence[name]) <= 4000,
                    "INVALID_QUALIFICATION", "Record the exact client/account/route profile before enabling writes", field=name)
        require(evidence["mode"] == "ordinary_chat" and evidence["role"] in {"operator", "author"},
                "INVALID_QUALIFICATION", "Qualification must name the actual ordinary-Chat write role")
        if "principal_id" in evidence:
            safe_id(evidence["principal_id"])
        identity = current_identity()
        if not identity.legacy:
            require(evidence.get("principal_id") == identity.principal_id, "INVALID_QUALIFICATION",
                    "A scoped principal must qualify its own identity explicitly")
        profile = evidence.get("output_profile", OUTPUT_PROFILE)
        require(profile in OUTPUT_PROFILES, "INVALID_QUALIFICATION", "Unknown output profile")
        record = {"qualification_id": "qualification_" + uuid.uuid4().hex,
                  "instance_id": self.store.identity["instance_id"], "actor": actor,
                  "created": utc_now(), "expires": utc_now() + 30 * 86400, "evidence": evidence,
                  "binding": catalog_identity(profile), "billing_verified": False,
                  "interpretation": "operator-recorded capability, not a universal account entitlement"}
        op, _ = self.store.begin_operation(kind="client.qualify", key=key or "local-" + uuid.uuid4().hex,
                    fingerprint=fingerprint({"evidence": evidence, "actor": actor, "binding": record["binding"]}), payload=record)
        with file_lock(self.store.home / "locks/client-qualification"):
            current = self.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
            if current["state"] == "completed":
                return json.loads(current["result"])
            record = json.loads(current["payload"])
            with self.store.transaction() as conn:
                marker = conn.execute("SELECT value FROM metadata WHERE key=?", (self._QUALIFICATION_REGISTRY,)).fetchone()
                if marker is None:
                    # First authorized write migrates the old singleton. Read-only
                    # catalogue/status calls never mutate or activate authority.
                    legacy = self._legacy_qualification()
                    if legacy is not None:
                        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (self._qualification_key(legacy), canonical(legacy).decode()))
                    conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (self._QUALIFICATION_REGISTRY, canonical({'version': 1}).decode()))
                else:
                    require(strict_json(marker[0]) == {'version': 1}, "CORRUPT_STATE", "Invalid qualification registry")
                conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (self._qualification_key(record), canonical(record).decode()))
                self.store.event(conn, "client.qualified", "", {"qualification_id": record["qualification_id"], "actor": actor})
                conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                             (canonical(record).decode(), utc_now(), op["id"]))
            return record

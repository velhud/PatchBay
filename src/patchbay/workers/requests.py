"""Optional retry identity in the existing durable worker job records.

No second journal and no replayed inference: a repeated admission is read back.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from contextvars import ContextVar

from patchbay.jobs.manager import JobInfo
from patchbay.ownership import owner_hash_for_context, owner_scope_for_context
from patchbay.repo_locks import fcntl

REQUEST_OPTION = '_worker_request'
_REQUEST = ContextVar('worker_request', default=None)


class WorkerAdmissionFault(RuntimeError):
    def __init__(self, details):
        super().__init__('Worker effect may already exist; recover its durable admission')
        self.details = details


def _reference(kind, context, key):
    scope = [kind, owner_scope_for_context(context), owner_hash_for_context(context), key]
    return hashlib.sha256(json.dumps(scope, separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()


def fault_details(runtime, kind, context, key, *, code='INVALID_OUTPUT'):
    data = {'code': code, 'boundary': 'effect_may_exist_result_unavailable',
            'effect_may_exist': True, 'success_asserted': False}
    actions = []
    if isinstance(key, str):
        try:
            key.encode('utf-8')
            bounded = 0 < len(key) <= 200
        except UnicodeError:
            bounded = False
        if bounded:
            data['original_key'] = key
            if runtime is not None and kind:
                ref = _reference(kind, context, key)
                try:
                    matches = [job for job in _durable_jobs(runtime.job_manager)
                               if (job.options or {}).get(REQUEST_OPTION, {}).get('ref') == ref]
                    if len(matches) == 1:
                        worker_id = runtime._worker_id(matches[0])
                        data.update(request_ref=ref, worker_id=worker_id)
                        actions.append({'tool': 'codex_worker_inspect', 'arguments': {'worker': worker_id, 'view': 'status'}})
                except Exception:
                    data['recovery_lookup_unavailable'] = True
    data['recovery_actions'] = actions or [{'tool': 'codex_worker_list', 'arguments': {'scope': 'all'}}]
    data['guidance'] = 'No success or absence of an effect is asserted. Recover existing work; keyed retries keep the exact key and arguments. Unkeyed calls have no replay guarantee. Do not use auto_suffix or a new key as a blind retry.'
    return data


def request_metadata():
    value = _REQUEST.get()
    return {REQUEST_OPTION: dict(value)} if value else {}


def _durable_jobs(manager):
    # Atomic existing job snapshots are the admission index. Corrupt state must
    # not be interpreted as absence of a potentially accepted request.
    jobs = []
    for path in sorted(manager.job_state_dir.glob('*.json')):
        if path.is_symlink():
            raise ValueError('Inspect unsafe durable job state before retrying')
        try:
            jobs.append(JobInfo.from_persisted_dict(json.loads(path.read_text(encoding='utf-8'))))
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ValueError('Inspect unreadable durable job state before retrying') from error
    return jobs


async def admitted_call(runtime, method, arguments, key):
    if key is None:
        token = _REQUEST.set(None)
        try:
            return await method(**arguments)
        finally:
            _REQUEST.reset(token)
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValueError('idempotency_key must be a non-empty UTF-8 string of at most 200 characters')
    try:
        key.encode('utf-8')
    except UnicodeError as error:
        raise ValueError('idempotency_key must be valid UTF-8') from error
    if fcntl is None:
        raise ValueError('Keyed local worker admission requires supported cross-process file locking')
    bound = inspect.signature(method).bind(**arguments)
    bound.apply_defaults()
    values = dict(bound.arguments)
    context = values.pop('request_context', None)
    # S06 adds an optional source selector; omission keeps pre-upgrade keys.
    if values.get('source_candidate') is None:
        values.pop('source_candidate', None)
    encoded = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    record = {'ref': _reference(method.__name__, context, key),
              'fingerprint': hashlib.sha256(encoded(values)).hexdigest(), 'kind': method.__name__}
    path = runtime.job_manager.job_state_dir / 'worker-admission.lock'
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.025)
        jobs = _durable_jobs(runtime.job_manager)
        matches = [job for job in jobs if (job.options or {}).get(REQUEST_OPTION, {}).get('ref') == record['ref']]
        if len(matches) > 1:
            raise ValueError('Conflicting durable admissions require inspection before retry')
        if matches:
            job = matches[0]
            if job.options[REQUEST_OPTION] != record:
                raise ValueError('idempotency_key already belongs to a different worker request')
            try:
                result = runtime._public_view([job], request_context=context, include_change_state=False)
            except Exception as error:
                raise WorkerAdmissionFault(fault_details(runtime, method.__name__, context, key)) from error
            result.update(accepted=True, request_replayed=True, request_ref=record['ref'],
                note='Original worker request was already admitted; no new turn was scheduled. This is its retained admission view; inspect the worker for current state.')
            return result
        token = _REQUEST.set(record)
        try:
            try:
                result = await method(**arguments)
            except Exception as error:
                details = fault_details(runtime, method.__name__, context, key, code='EFFECT_OUTCOME_UNAVAILABLE')
                if 'worker_id' in details:
                    raise WorkerAdmissionFault(details) from error
                raise
        finally:
            _REQUEST.reset(token)
        if result.get('accepted'):
            result = {**result, 'request_replayed': False, 'request_ref': record['ref']}
        return result
    finally:
        os.close(descriptor)

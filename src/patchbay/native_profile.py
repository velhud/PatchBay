"""Passive, bounded installation/profile identity. Never invoke a native CLI.

Static manifest matching is not a model canary, workflow or client pass. This
module has no worker, provider, Author or application-startup dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat


MAX_FILES = 256
MAX_MANIFEST_BYTES = 128*1024
MAX_BUNDLE_BYTES = 512*1024*1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def stat_binding(path):
    path = Path(path).resolve(strict=True)
    info = path.stat()
    return digest([str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])


def read_object(path):
    with open_regular(path) as handle:
        data = handle.read(MAX_MANIFEST_BYTES+1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError('Native manifest/receipt exceeds policy')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('Native manifest/receipt must be an object')
    return value


def open_regular(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError('Native identity input must be a regular file')
    return os.fdopen(descriptor, 'rb')


def check_installation(root, manifest, *, executable):
    """Explicit operator action against a supplied distribution manifest.

    The manifest's distribution/provenance declaration is retained, not
    independently authenticated. Required companions must be declared from
    the exact distribution; no guessed universal companion layout is used.
    """
    root = Path(root).resolve(strict=True)
    executable = Path(executable).resolve(strict=True)
    files = manifest.get('files')
    if manifest.get('format') != 'patchbay-native-distribution-v1' or not isinstance(files,list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError('Invalid bounded native distribution manifest')
    records, problems, seen = [], [], set()
    total = 0
    for item in files:
        name = item.get('path') if isinstance(item,dict) else None
        if not isinstance(name,str) or not name or len(name)>1024:
            raise ValueError('Invalid native distribution path')
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or '\0' in name or name in seen:
            raise ValueError('Unsafe or duplicate native distribution path')
        seen.add(name)
        path = root/name
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError('Native distribution path escapes its root')
        try:
            size = path.stat().st_size
            total += size
            if total > MAX_BUNDLE_BYTES or not path.is_file():
                raise ValueError('Native distribution exceeds file/byte policy')
            hasher = hashlib.sha256()
            with open_regular(path) as handle:
                remaining = size
                while remaining:
                    block = handle.read(min(1024*1024, remaining))
                    if not block:
                        raise ValueError('Native distribution changed during validation')
                    hasher.update(block); remaining -= len(block)
                if handle.read(1):
                    raise ValueError('Native distribution changed during validation')
            valid = hasher.hexdigest() == item.get('sha256')
            if item.get('executable') and not os.access(path, os.X_OK):
                valid = False
            records.append(dict(path=name, stat_binding=stat_binding(path), sha256=hasher.hexdigest(),
                                role=str(item.get('role') or 'companion')[:80]))
            if not valid:
                problems.append(dict(path=name, reason='hash_or_executable_mismatch'))
        except OSError:
            problems.append(dict(path=name, reason='required_file_unavailable'))
    launchers = [row for row in records if (root/row['path']).resolve() == executable and row['role']=='launcher']
    if not launchers:
        problems.append(dict(reason='resolved_launcher_not_in_manifest'))
    binding = dict(root=str(root), files=records, executable=str(executable))
    public = dict(format='patchbay-native-installation-v1',
        status='failed' if problems else 'matched_supplied_manifest',
        manifest_sha256=digest(manifest), declared_distribution_version=str(manifest.get('version') or '')[:80],
        declared_manifest_provenance=str(manifest.get('provenance') or 'unspecified')[:300],
        file_count=len(records), bytes=total, problems=problems,
        native_execution_qualified=False, workflow_qualified=False, client_qualified=False,
        nested_helpers_qualified=False, external_session_use_qualified=False,
        session_attribution='managed_single_owner_required')
    public['installation_id'] = digest([public, binding])
    return {**public, 'private_binding':binding}


def receipt_status(receipt_path, executable):
    if not receipt_path:
        return dict(status='not_checked', installation_id=None)
    try:
        receipt = read_object(receipt_path)
        binding = receipt['private_binding']
        rows = binding['files']
        valid = (receipt.get('format') == 'patchbay-native-installation-v1'
            and receipt.get('status') == 'matched_supplied_manifest'
            and Path(binding['executable']).resolve() == Path(executable).resolve()
            and isinstance(rows,list) and 1 <= len(rows) <= MAX_FILES)
        root = Path(binding['root']).resolve(strict=True)
        for row in rows:
            path = root/row['path']
            valid = valid and not path.is_symlink() and root in path.resolve().parents
            valid = valid and stat_binding(path) == row['stat_binding']
        return dict(status='current_static_binding' if valid else 'stale',
                    installation_id=receipt.get('installation_id'))
    except (OSError, ValueError, KeyError, TypeError):
        return dict(status='stale_or_unavailable', installation_id=None)


def launch_profile(command, *, environment, cwd, config, schema):
    """Pin actual executable resolution and record requested versus unknown facts."""
    def argument(name):
        try:
            return command[command.index(name)+1]
        except (ValueError, IndexError):
            return None

    workspace = Path(argument('--cd') or cwd)
    if not workspace.is_absolute():
        workspace = Path(cwd)/workspace
    workspace = workspace.resolve()
    resolved = shutil.which(command[0], path=environment.get('PATH'))
    if not resolved:
        raise FileNotFoundError('The selected native executable is unavailable')
    launcher = Path(resolved).absolute()
    executable = launcher.resolve(strict=True)
    home = Path(environment['CODEX_HOME']).resolve()
    configuration = []
    for name, path in [('home/config.toml',home/'config.toml'), ('home/AGENTS.md',home/'AGENTS.md'),
                       ('workspace/AGENTS.md',workspace/'AGENTS.md'), ('workspace/.codex/config.toml',workspace/'.codex/config.toml')]:
        try:
            with open_regular(path) as handle:
                data = handle.read(1024*1024+1)
            configuration.append((name, hashlib.sha256(data).hexdigest() if len(data)<=1024*1024 else 'over_limit'))
        except (OSError, ValueError):
            configuration.append((name, 'unavailable'))
    rule_bytes = 0
    for label, root in [('home',home/'rules'),('workspace',workspace/'.codex/rules')]:
        for index, path in enumerate(root.rglob('*.rules')):
            if index >= MAX_FILES or rule_bytes > 4*1024*1024:
                configuration.append((label, 'rules_inventory_over_limit'))
                break
            try:
                with open_regular(path) as handle:
                    data = handle.read(1024*1024+1)
                rule_bytes += len(data)
                configuration.append((label+'/'+str(path.relative_to(root)),hashlib.sha256(data).hexdigest()))
            except (OSError, ValueError):
                configuration.append((label+'/'+str(path.relative_to(root)),'unavailable'))
    configuration.sort()
    overrides = [command[n+1] for n,item in enumerate(command[:-1]) if item == '-c']
    effort = next((s.partition('=')[2].strip('"\'') for s in overrides if s.startswith('model_reasoning_effort=')), None)
    public = dict(version=1, adapter='exec-jsonl-v1', executable_binding=stat_binding(executable),
        launcher_id=digest(str(launcher)),
        home_id=digest(str(home)), workspace_id=digest(str(workspace)),
        process_cwd_id=digest(str(Path(cwd).resolve())),
        configuration_sha256=digest([configuration, overrides, argument('--profile'),
            {key:environment.get(key) for key in ('PATH','HOME','CODEX_HOME','XDG_CONFIG_HOME','GIT_CONFIG_GLOBAL')}]),
        output_schema_sha256=hashlib.sha256(Path(schema).read_bytes()).hexdigest(),
        argv_sha256=digest([str(launcher),*command[1:]]), requested_model=argument('--model'), requested_reasoning_effort=effort,
        observed_model=None, observed_reasoning_effort=None, observed_session_id=None,
        static_installation=receipt_status(config.get('server',{}).get('native_installation_receipt'),executable),
        native_execution_qualified=False, workflow_qualified=False, client_qualified=False,
        nested_helpers_qualified=False, external_session_use_qualified=False,
        session_attribution='managed_single_owner_required')
    return [str(launcher), *command[1:]], public

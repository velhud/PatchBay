# Author Mode qualification and Linux fault lab

## Portable checks

The [MCP interface contract](../reference/mcp-interface-contract.md) defines
selector, role, version, result-fault and exact-client qualification cases.
Deterministic protocol fixtures are local evidence only. Schema, instructions,
runtime and the shared version helper participate in Author qualification
identity, so upgrading these files requires a fresh observed route binding.

Run from the repository root:

```bash
python -m compileall -q src scripts tests
python -m pytest tests/authoring -q
python scripts/author_requirements_eval.py --json
python scripts/author_interface_eval.py --json
```

Portable Author tests exercise exact state, candidate refs, replay, fail-closed
backup/restore, blob verification and reference-aware GC. Linux-only cases also
exercise real disposable supervisors and loopback endpoints when actually run
on Linux; portable-only passes do not qualify those paths. Check the pytest
report for platform skips and retain them as `BLOCKED_ENVIRONMENT`, never as
successful cases. [S07 evidence semantics](../repairs/2026-10-22-s07-evidence-authority.md)
separate executor observations, program reports and independent acceptance.

## Evidence format

Create a private JSON file with `schema_version: 1` and a `runs` object keyed
by scenario IDs. Each run records `status` (`PASS`, `FAIL`, `NOT_RUN`, or
`BLOCKED_ENVIRONMENT`), `commit`, exact `command`, a short `reason` when not
passing, and `artifacts` containing paths and SHA-256 values. Ordinary Chat
cases additionally require `execution_surface: ordinary_chat` and a
`profile_id`. The evaluator checks that a claimed pass is pinned to current
`HEAD`, that files exist and hash correctly, and that the ordinary-Chat fields
exist. It cannot establish that an external trial was honestly executed; retain
the raw client/server evidence for review. Keep auth material and private
profile details out of public output.

```bash
python scripts/author_requirements_eval.py --evidence /secure/path/qualification.json --json
```

The evaluator's source of case identity is the checked-in
[`reliability-acceptance-tests.json`](reliability-acceptance-tests.json), an
unaltered 52-case copy of
`work/reliability-package/registers/acceptance-tests.json`, SHA-256
`f05b0ec4c533f5c119237ad05ad9d96d16506014b19a35abad9b6b574ab58600`. Each
output case retains its source `status` (`NOT_RUN_IN_THIS_RESEARCH` in that
register) and adds an independent `evaluated_status`. Original IDs and titles
are not relabeled. See the [source-pinned coverage map](coverage-map.md) for
code/test correspondences and their limits.

The golden corpus is used for paired trials in fresh ordinary Chat sessions.
Freeze model, account/workspace, client, route, catalog, instructions, task
fixture and server candidate. Change one dimension at a time. Capture JSONL
records with `case_id`, `variant` (`full-text+structured` or
`concise+structured`), explicit `status`, raw `input_raw`/`output_raw`, observed
`tool_ids`, `required_tool_ids`, `execution_surface: "ordinary_chat"` and
`profile_id`. Run:

```bash
python scripts/author_interface_eval.py --capture /secure/path/ab-capture.jsonl --json
```

The evaluator compares case pairing, recorded status, mechanically observed
tool-ID coverage and UTF-8 byte lengths. Missing variants remain `NOT_RUN`;
actual A/B outcomes remain `NOT_RUN` unless every included golden case has
both variants observed in ordinary Chat with profile IDs. Bytes do not establish token counts,
cost or model selection. Independently score useful/correct calls, unnecessary
calls, missed actions, authority violations, clarification behavior and
completion quality by a human. Preserve failures and exclusions. No static
rule or evaluator result is a measure of model judgment.

## Disposable Linux fault lab

Run only in a disposable Linux VM with a disposable state root and repository.
Never run reboot, filesystem exhaustion, power-loss or stress commands on a
production host. Do not install a VM or runtime as part of this qualification.
Pin the VM image, kernel, filesystem, storage mount, Docker image digest and
resource limits in the profile. Save pre-fault backup, journal snapshot, exact
operation ID and expected acknowledged/unacknowledged boundary.

### Process restart and reboot

Run the service under a dedicated systemd unit and place the disposable
operation in a controlled long-running check. Capture unit/cgroup identity and
operation receipts, then restart only the endpoint:

```bash
systemctl show patchbay-author-lab.service -p MainPID -p ControlGroup -p ActiveState
systemctl restart patchbay-author-lab.service
systemctl show patchbay-author-lab.service -p MainPID -p ControlGroup -p ActiveState
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" status OPERATION_ID
cat /proc/sys/kernel/random/boot_id
```

Repeat with `systemctl reboot` only in the disposable VM. After boot, verify
boot identity, durable task/revision/operation records, owned process identity,
candidate Git refs and receipts. An interrupted effect with no proof must be
classified unknown/fenced. A reused PID must never count as continuity.

### Disk-full and interruption boundaries

Use a throwaway loopback filesystem mounted only inside the VM. Run only after
checking the lab marker and confirming the mount point is the disposable test
mount. Point only the test service's Author home at it:

```bash
test -f /etc/patchbay-disposable-lab
test "$LAB_MOUNT" = /mnt/patchbay-author-lab
sudo mkdir -p "$LAB_MOUNT"
sudo truncate -s 512M /var/tmp/patchbay-author-lab.img
sudo mkfs.ext4 -F /var/tmp/patchbay-author-lab.img
sudo mount -o loop /var/tmp/patchbay-author-lab.img "$LAB_MOUNT"
export PATCHBAY_AUTHOR_HOME="$LAB_MOUNT/state"
df -h "$LAB_MOUNT"
df -i "$LAB_MOUNT"
```

Create one named fill file sized for the configured state budget and available
space, then exercise state write, blob ingest, backup publication and cleanup
independently:

```bash
fallocate -l 440M "$LAB_MOUNT/qualification-fill.bin"
df -h "$LAB_MOUNT"
df -i "$LAB_MOUNT"
```

Record the exact operation/result at every failure. Reclaim only this fixture
file after recording the rejection, run `patchbay-author --home
"$PATCHBAY_AUTHOR_HOME" doctor`, then test restore and GC. Do not use `fallocate`
or `dd` against a host or production filesystem.

For state-boundary faults, use test hooks around intent commit, candidate blob
write, Git ref creation, operation receipt and terminal response. Kill only the
service process at one documented hook per run. After each restart, assert:
acknowledged intent is discoverable; exact replay returns the original
operation/result; an unacknowledged/uncertain effect is not silently replayed;
candidate blob hash and Git ref remain consistent; and corrupted backup/state
fails closed without creating a runtime. Preserve before/after DB snapshots and
Git refs.

### Offline Docker and saturation

Prepare and verify the exact Docker image digest before the test. Disable
network for the verification container, supply no host credential or Docker
socket mount, and record inspect output. For the offline case, block egress at
the disposable VM by lowering its test interface, and do not rely only on an
application `--network=none` flag to prove the host had no network. Confirm the
image is already present, then run the strict test:

```bash
test -f /etc/patchbay-disposable-lab
docker image inspect "$PATCHBAY_AUTHOR_DOCKER_IMAGE" >/dev/null
docker image inspect --format '{{index .RepoDigests 0}}' "$PATCHBAY_AUTHOR_DOCKER_IMAGE"
restore_lab_nic() { sudo ip link set dev "$LAB_NIC" up; }
trap 'restore_lab_nic' EXIT
trap 'restore_lab_nic; exit 130' INT
trap 'restore_lab_nic; exit 143' TERM
sudo ip link set dev "$LAB_NIC" down
PATCHBAY_AUTHOR_LONG_TESTS=1 python -m pytest tests/authoring/test_security_contracts.py -q
restore_lab_nic
trap - EXIT INT TERM
```

Record that the image was present before the network cut and confirm the engine
made no pull attempt. Before running, verify with `ip -brief link show
"$LAB_NIC"` that this is the disposable VM's designated test interface from
the lab profile; never select a host/management interface. The EXIT/INT/TERM
traps restore it after test failure or interruption.

Run CPU, memory, process and log pressure inside cgroup-limited throwaway jobs.
While saturated, issue status, cancel and recovery reads from a reserved control
client. Record queue/admission result, peak RSS/CPU/PIDs/log bytes and control
latency. The pass condition is truthful admission or explicit rejection while
control reads remain serviceable; do not imply production capacity from one VM.
For a prepared lab with `stress-ng`, run pressure in a separate terminal:

```bash
sudo systemd-run --scope --unit=patchbay-lab-saturation \
  -p CPUQuota=150% -p MemoryMax=1G -p TasksMax=128 \
  stress-ng --cpu 4 --vm 2 --vm-bytes 700M --timeout 5m --metrics-brief
```

From the reserved control terminal, run the documented read-only status and
operation-recovery calls for the exact disposable operation ID while the scope
is active. Save timestamps and response latencies. Stop only the named scope
after capturing its final state.

### Abrupt power-loss class

Only the hypervisor's disposable-VM hard power-off is in scope; do not cut power
to the physical host. Record database synchronous mode, filesystem and virtual
disk cache policy. Power off at the documented acknowledged boundary, reboot,
then verify DB integrity, blobs, Git refs, operation classification and backup
restoration. State the observed boundary precisely. A process-kill test does
not qualify host crash or power-loss durability.

For a libvirt VM, confirm the exact disposable domain before this command:

```bash
virsh dominfo "$PATCHBAY_LAB_VM"
virsh destroy "$PATCHBAY_LAB_VM"
virsh start "$PATCHBAY_LAB_VM"
```

`virsh destroy` is the abrupt power-off injection. Never substitute the host or
any non-disposable domain name.

## Current qualification status

The source has portable crash/replay/GC/fail-closed regression cases. Linux
restart, reboot, disk-full, offline Docker, saturation and hard-power-off are
separate lab runs. Ordinary Chat A/B, account route, metadata refresh and
human-scored prompt selection are also not proven by local tests. The evaluator
shows absent runs explicitly; do not report a release-qualified profile until
the required environment-specific evidence exists.

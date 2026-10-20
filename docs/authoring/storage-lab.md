# Author storage fault lab

This opt-in Linux check uses one private mount namespace and a dedicated tmpfs
limited to 64 MiB and 2,048 inodes. It exercises real `ENOSPC` at package
acceptance, receipt persistence, backup publication, restore extraction, and
inode allocation. It then frees only its named fixture files, retries durable
operations, restores a backup, and checks replay and reference-aware GC.

From the repository root, run:

```bash
python scripts/author_storage_lab.py
```

The runner invokes `sudo /usr/local/bin/codex-host-admin unshare --mount
--propagation private`, mounts tmpfs inside that private namespace, and runs
only `tests/authoring/test_storage_lab.py` with pytest's temporary directory
root on the mount. The namespace exits and removes the mount after the run.
Raw command output and a JSON record with the commit, kernel, source hashes,
namespace identity, observed limits, and exit status are saved outside tmpfs
under `.local/validation/storage-lab/`. Use `--raw-dir PATH` to select a
separate private evidence directory.

The test is skipped in ordinary Author runs. The actual exhaustion proof is
the marked test inside this runner; the pre-existing monkeypatched `ENOSPC`
unit tests do not count as real filesystem faults. If the mount namespace or
tmpfs setup fails, the runner records that failure and does not fall back to
the host filesystem.

The lab covers process-level filesystem failures and exception cleanup. It is
not a power-loss, reboot, physical-disk, host-saturation, process-supervisor,
or full original T37/T38 qualification. A failed assertion is retained in the
raw log. In particular, a leftover backup `.part` after failed publication is
reported as a persistence maintenance defect; the test removes only that
named lab output so later restore and retention checks can continue.

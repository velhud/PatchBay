"""Real Linux processes and loopback HTTP, with only disposable owned work.

These tests establish local endpoint/process behavior, never ordinary Chat,
production capacity, reboot, or physical power-loss qualification.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.identity import identity, same_process, signal_exact
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.models import Check
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.util import canonical
from test_execution import wait_for
from test_runner_guards import configured


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Real Linux process ownership and HTTP qualification")


class Endpoint:
    def __init__(self, home, root):
        self.home, self.root, self.process = home, root, None
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.token = json.loads((home / "tokens.json").read_text())["operator"]
        self.logs = []

    def start(self):
        log = (self.root / f"endpoint-{len(self.logs)}.log").open("wb")
        self.logs.append(log)
        program = ("import sys,uvicorn;from pathlib import Path;"
                   "from patchbay.authoring.interfaces.http import create_app;"
                   "uvicorn.run(create_app(Path(sys.argv[1])),fd=int(sys.argv[2]),access_log=False,log_level='warning')")
        self.process = subprocess.Popen([sys.executable, "-c", program, str(self.home), str(self.socket.fileno())],
                                        pass_fds=(self.socket.fileno(),), stdout=log, stderr=log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.process.poll() is None, "Disposable endpoint exited; inspect endpoint log"
            try:
                self.request("/api/capabilities")
                return
            except (OSError, TimeoutError):
                time.sleep(.05)
        pytest.fail("Disposable endpoint did not become ready")

    def request(self, path, value=None):
        request = urllib.request.Request(self.url + path, data=canonical(value) if value is not None else None,
                                         headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def close(self):
        self.stop()
        self.socket.close()
        for log in self.logs:
            log.close()


def test_endpoint_restart_and_control_under_actual_bounded_job_saturation(setup_author, tmp_path):
    # Two finite jobs reserve the fixture's two slots. Each allocates 32 MiB,
    # emits bounded output and intermittently computes; no host stress tool.
    program = ("import time\nfrom pathlib import Path\n"
               "data=bytearray(32*1024**2)\n"
               "Path('.author-reports').mkdir(exist_ok=True)\n"
               "Path('.author-reports/ready').write_text('ready')\n"
               "deadline=time.monotonic()+30\n"
               "while time.monotonic()<deadline:\n"
               " end=time.monotonic()+.05\n"
               " while time.monotonic()<end: value=sum(range(1000))\n"
               " print('fixture progress',flush=True);time.sleep(.1)\n")
    service, _, task, lease = configured(setup_author, "local-control-lab",
        [Check(id="bounded", argv=[sys.executable, "-c", program], timeout_seconds=40)],
        cpu=.75, memory_mb=128, disk_mb=32, log_bytes=4096)
    checks = Checks(service.store)
    endpoint = Endpoint(service.store.home, tmp_path)
    try:
        endpoint.start()
        first = checks.submit(task["id"], revision=0, key="active-one", token=lease["token"], epoch=lease["epoch"])
        second = checks.submit(task["id"], revision=0, key="active-two", token=lease["token"], epoch=lease["epoch"])
        for operation in (first, second):
            ready = service.store.home / "runs" / operation["id"] / "source/.author-reports/ready"
            deadline = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert ready.exists()
        queued = checks.submit(task["id"], revision=0, key="queued-three", token=lease["token"], epoch=lease["epoch"])
        assert queued["state"] == "queued" and queued["phase"] == "waiting_for_admission"
        assert queued["admission"]["reason"] == "resource_budget"
        assert queued["admission"]["admission"]["gates"]["job_slots"] is False
        owned = json.loads((service.store.home / "runs" / first["id"] / "launch.json").read_text())["supervisor"]
        old_endpoint = identity(endpoint.process.pid)
        endpoint.stop()
        assert same_process(owned)
        endpoint.start()
        assert not same_process(old_endpoint)
        assert same_process(owned)
        assert endpoint.request("/api/operations/" + first["id"])["id"] == first["id"]
        events = service.store.one("SELECT COUNT(*) AS n FROM events")["n"]
        latencies = []
        for _ in range(3):
            started = time.monotonic()
            endpoint.request("/api/tasks/" + task["id"] + "/recovery")
            latencies.append(time.monotonic() - started)
        checks.dispatch(operation_ids=[queued["id"]])
        assert service.store.one("SELECT COUNT(*) AS n FROM events")["n"] == events
        assert not (service.store.home / "runs" / queued["id"]).exists()
        for operation in (first, second, queued):
            started = time.monotonic()
            endpoint.request("/api/cancel", {"operation_id": operation["id"], "token": lease["token"],
                             "epoch": lease["epoch"], "key": "cancel-" + operation["id"]})
            latencies.append(time.monotonic() - started)
            assert wait_for(service, operation)["state"] == "cancelled"
        # The newest queued cancellation correctly has no execution receipt.
        # Inspect the exact run whose live supervisor crossed the restart.
        receipt_sha = service.store.one("SELECT receipt_sha FROM runs WHERE id=?", (first["id"],))["receipt_sha"]
        receipt = json.loads(service.store.blobs.read(receipt_sha))
        assert receipt["cleanup"] == "proven"
        assert receipt["resource_usage"]["samples"] > 0
        assert receipt["resource_usage"]["rss_bytes_peak"] >= 32 * 1024**2
        assert not same_process(owned)
        (tmp_path / "linux-control-evidence.json").write_bytes(canonical({
            "execution_surface": "local_loopback_http", "operations": [o["id"] for o in (first, second, queued)],
            "supervisor_before_restart": owned, "endpoint_before_restart": old_endpoint,
            "endpoint_after_restart": identity(endpoint.process.pid), "queued_admission": queued["admission"],
            "control_latency_seconds": latencies, "resource_usage": receipt["resource_usage"],
            "cleanup": receipt["cleanup"], "ordinary_chat": False}))
    finally:
        endpoint.close()


@pytest.mark.parametrize("mismatch", ["start", "boot"])
def test_recycled_or_old_boot_identity_cannot_signal_or_authorize_relaunch(setup_author, tmp_path, mismatch):
    service, _, _, task, lease = setup_author
    owned_fixture = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    try:
        record = identity(owned_fixture.pid)
        record[mismatch] = str(int(record["start"]) + 1) if mismatch == "start" else "different-boot-fixture"
        assert not same_process(record)
        assert signal_exact(record, 15) is False
        operation = Checks(service.store).submit(task["id"], revision=0, key="unowned", token=lease["token"],
                                                 epoch=lease["epoch"], dispatch=False)
        with service.store.transaction() as conn:
            conn.execute("UPDATE runs SET state='launching',cleanup='pending' WHERE id=?", (operation["id"],))
            conn.execute("UPDATE operations SET state='running',owner_identity=? WHERE id=?", (canonical(record).decode(), operation["id"]))
        assert Checks(service.store).recover(operation["id"])["state"] == "unknown_execution"
        assert Checks(service.store).dispatch(operation_ids=[operation["id"]])["launched"] == []
        assert owned_fixture.poll() is None
        with pytest.raises(AuthorError, match="active/uncertain"):
            Maintenance(service.store).backup(tmp_path / "must-not-exist.zip")
        assert not (tmp_path / "must-not-exist.zip").exists()
        (tmp_path / f"identity-{mismatch}-evidence.json").write_bytes(canonical({"forged_identity": record,
            "actual_identity": identity(owned_fixture.pid), "operation_id": operation["id"], "state": "unknown_execution"}))
    finally:
        owned_fixture.terminate()
        owned_fixture.wait(timeout=5)

from __future__ import annotations

import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.models import Check, Profile, TaskPolicy
from patchbay.authoring.packages.archive import PackageReader
from patchbay.authoring.packages.ingestion import NativeDownloader
from patchbay.authoring.util import strict_json
from test_execution import wait_for
from test_core import changed_archive, package_for, submit


def test_duplicate_json_keys_and_nonfinite_numbers_rejected():
    for value in ('{"version":1,"version":2}', '{"number":NaN}', '{"number":Infinity}'):
        with pytest.raises(AuthorError): strict_json(value)


def test_archive_symlinks_are_not_extracted(setup_author, tmp_path):
    s, _, _, _, _ = setup_author
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as target:
        info = zipfile.ZipInfo("payload/link"); info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(info, "../../outside")
        target.writestr("manifest.json", "{}")
    with pytest.raises(AuthorError): PackageReader(s.store).read(archive)
    assert not (tmp_path / "outside").exists()


def test_unknown_check_id_cannot_inject_a_command(setup_author, tmp_path):
    s, _, _, task, lease = setup_author
    package = package_for(s, task, tmp_path, {"added.txt": "change"})
    altered = changed_archive(package, tmp_path / "injected.zip", lambda manifest, members: manifest.update(requested_checks=["python -c 'print(1)' "]))
    with pytest.raises(AuthorError) as error: submit(s, task, altered, lease)
    assert error.value.code == "POLICY_DENIED"
    assert s.tasks.get(task["id"])["current_revision"] == 0


def test_native_file_transport_rejects_private_and_unapproved_urls(setup_author, monkeypatch):
    s, _, _, _, _ = setup_author
    downloader = NativeDownloader(s.store)
    with pytest.raises(AuthorError): downloader.validate_url("http://127.0.0.1/package.zip")
    with pytest.raises(AuthorError): downloader.validate_url("https://unapproved.example/package.zip")
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(AuthorError): downloader.validate_url("https://files.oaiusercontent.com/file")


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_nonzero_exit_is_not_green_even_with_passing_junit(setup_author):
    s, source, _, _, _ = setup_author
    program = "from pathlib import Path; Path('.author-reports').mkdir(); Path('.author-reports/j.xml').write_text('<testsuite><testcase name=\"fake-green\"/></testsuite>'); raise SystemExit(9)"
    profile = Profile(id="exit-over-report", backend="trusted-local", cpu=.25, memory_mb=256, disk_mb=128,
                      checks=[Check(id="unit", argv=[sys.executable, "-c", program], report="junit", report_path=".author-reports/j.xml")])
    repo = s.repositories.register("exit-over-report", source, TaskPolicy(profile=profile))
    task = s.tasks.create(repo["id"], "Exercise raw-exit precedence", key="task")
    lease = s.leases.claim(task["id"])
    op = Checks(s.store).submit(task["id"], revision=0, key="run", token=lease["token"], epoch=lease["epoch"])
    assert wait_for(s, op)["state"] == "failed"
    assert Feedback(s.store).get(task["id"])["classification"] == "CHECKS_FAILED"


@pytest.mark.skipif(not os.environ.get("PATCHBAY_AUTHOR_DOCKER_IMAGE"), reason="Requires an explicitly prepared local Docker image")
@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_strict_docker_denies_network_and_host_auth_environment(setup_author, monkeypatch):
    s, source, _, _, _ = setup_author
    monkeypatch.setenv("AUTHOR_TEST_MUST_NOT_LEAK", "host-only-value")
    program = """import os,socket
from pathlib import Path
assert 'AUTHOR_TEST_MUST_NOT_LEAK' not in os.environ
assert not Path('/var/run/docker.sock').exists()
s=socket.socket();s.settimeout(1)
try:
    s.connect(('1.1.1.1',53))
except OSError:
    pass
else:
    raise AssertionError('Network was available in strict verification')
finally:
    s.close()
"""
    profile = Profile(id="strict", backend="docker", image=os.environ["PATCHBAY_AUTHOR_DOCKER_IMAGE"], cpu=.25, memory_mb=256, disk_mb=128,
                      checks=[Check(id="isolation", argv=["python", "-c", program], timeout_seconds=30)])
    repo = s.repositories.register("strict", source, TaskPolicy(profile=profile))
    task = s.tasks.create(repo["id"], "Explicit isolated backend qualification", key="strict")
    lease = s.leases.claim(task["id"])
    op = Checks(s.store).submit(task["id"], revision=0, key="run", token=lease["token"], epoch=lease["epoch"])
    assert wait_for(s, op, timeout=60)["state"] == "completed"
    assert Feedback(s.store).get(task["id"])["receipt"]["environment"]["network_denied_during_checks"] is True

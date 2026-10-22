"""Count real census owner invocations with a deterministic fake ps result."""
import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
import patchbay.jobs.executor as implementation
from test_worker_projection_v2 import make_config


@pytest.mark.asyncio
async def test_routine_census_is_coalesced_and_forced_cleanup_is_fresh(tmp_path,monkeypatch):
    config=make_config(tmp_path)
    executor=JobExecutor(config,JobManager(config))
    class NoProc:
        def is_dir(self):return False
    original_path=implementation.Path
    monkeypatch.setattr(implementation,'Path',lambda value:NoProc() if str(value)=='/proc' else original_path(value))
    scans=[]
    complete=[True]
    def census(command,**kwargs):
        assert command==['ps','-axo','pid=,ppid=,stat=']
        scans.append(threading.get_ident())
        time.sleep(.04)
        return subprocess.CompletedProcess(command,0 if complete[0] else 1,'100 1 S\n','')
    monkeypatch.setattr(implementation.subprocess,'run',census)
    results=await asyncio.gather(*(asyncio.to_thread(executor._process_parent_snapshot) for _ in range(10)))
    assert results==[{100:1}]*10 and len(scans)==1
    assert all(identity!=threading.get_ident() for identity in scans)
    # Correctness barriers always bypass the ordinary sample.
    assert executor._process_parent_snapshot(force_refresh=True)=={100:1}
    assert len(scans)==2
    complete[0]=False
    assert executor._process_parent_snapshot(force_refresh=True) is None
    assert len(scans)==3

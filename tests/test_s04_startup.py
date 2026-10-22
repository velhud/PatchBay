"""Real owned subprocess/pipe boundaries with no native executable or provider."""
import asyncio
import os
import sys

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from test_session_terminal import make_config


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['never_ready', 'eof', 'invalid', 'cancel_no_serialization'])
async def test_pre_ready_lifecycle_is_bounded_and_never_releases_target(tmp_path, monkeypatch, case):
    config = make_config(tmp_path)
    config['server'].update(codex_supervisor_ready_timeout_seconds=.2,
        codex_startup_serialization_enabled=case != 'cancel_no_serialization')
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    marker = tmp_path/'native-target-must-not-run'
    job_id = manager.create_job('plan', 'startup', config['repositories']['default'], {})
    monkeypatch.setattr(executor, '_build_codex_command', lambda *a:
        [sys.executable, '-c', 'from pathlib import Path; Path('+repr(str(marker))+').touch()'])
    real_spawn = asyncio.create_subprocess_exec
    owned = []
    async def spawn(*command, **kwargs):
        ready_fd = int(command[command.index('--ready-fd')+1])
        script = ('import os,time\n' +
            ('os.write('+str(ready_fd)+",b'X')\n" if case == 'invalid' else '') +
            ('pass\n' if case == 'eof' else 'time.sleep(30)\n'))
        process = await real_spawn(sys.executable, '-u', '-c', script, **kwargs)
        owned.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    task = executor.schedule_job(job_id)
    try:
        if case == 'cancel_no_serialization':
            for _ in range(100):
                if owned:
                    break
                await asyncio.sleep(.01)
            assert owned
            result = await executor.cancel_job(job_id)
            assert result['cancelled']
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
        job = manager.get_job(job_id)
        assert job.state == (JobState.CANCELLED if case == 'cancel_no_serialization' else JobState.FAILED)
        assert all(p.returncode is not None for p in owned)
        assert not marker.exists()
        assert not job.options['_supervisor_target_gate_committed']
        assert not executor._terminal_cleanup_pending(job)
    finally:
        # Only exact unreaped handles created by this fixture are eligible.
        for process in owned:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if not task.done():
            task.cancel()
            await asyncio.wait_for(asyncio.shield(task), timeout=3)

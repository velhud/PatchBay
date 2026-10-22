"""Actual local ingress and durable jobs; executors record and never launch Codex."""
import asyncio
import json
import os
import subprocess
import sys
import yaml

import pytest
from fastapi.testclient import TestClient

from patchbay.jobs.manager import JobManager
from patchbay.protocol.mcp import MCPProtocol
from patchbay.stdio import StdioMCPServer
from patchbay.tools.handler import ToolHandler
from test_worker_runtime import RecordingExecutor, make_config


@pytest.mark.parametrize('transport', ['http', 'stdio'])
@pytest.mark.parametrize('framing', ['missing', None, True, [], {}, pytest.param('\ud800', id='invalid-unicode')])
def test_invalid_or_idless_call_cannot_admit_a_local_worker(tmp_path, monkeypatch, transport, framing):
    monkeypatch.setenv('PATCHBAY_HOME', str(tmp_path / 'private-runtime'))
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    protocol = MCPProtocol(config, ToolHandler(config, manager, executor))
    message = {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {
        'name': 'codex_worker_start', 'arguments': {'name': 'framing fixture',
        'brief': 'No inference execution permitted', 'workspace_mode': 'read_only'}}}
    if framing != 'missing':
        message['id'] = framing
    if transport == 'http':
        from patchbay import server
        monkeypatch.setattr(server, 'sessions', {})
        monkeypatch.setattr(server, 'work_runs', {})
        monkeypatch.setattr(server, 'mcp_protocol', protocol)
        monkeypatch.setattr(server.evidence_recorder, 'record_mcp_event', lambda **kwargs: None)
        client = TestClient(server.app)
        initialized = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        response = client.post('/mcp', content=json.dumps(message).encode('utf-8'),
            headers={'Mcp-Session-Id': initialized.headers['Mcp-Session-Id']})
        if framing == 'missing':
            assert response.status_code == 202
        else:
            assert response.json()['error']['code'] == -32600
    else:
        # Retain real line parsing/context/dispatch, with the same safe executor.
        endpoint = object.__new__(StdioMCPServer)
        endpoint.protocol = protocol
        endpoint.request_context = lambda: None
        response = asyncio.run(endpoint.handle_line(json.dumps(message)))
        if framing == 'missing':
            assert response is None
        else:
            assert response['error']['code'] == -32600
    assert not manager.jobs
    assert not list(manager.job_state_dir.glob('*.json'))
    assert not executor.started


def test_stdio_batch_is_rejected_before_any_member_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv('PATCHBAY_HOME', str(tmp_path / 'private-runtime'))
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    endpoint = object.__new__(StdioMCPServer)
    endpoint.protocol = MCPProtocol(config, ToolHandler(config, manager, executor))
    endpoint.request_context = lambda: None
    message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'codex_worker_start', 'arguments': {'name': 'batch fixture', 'brief': 'No inference',
        'workspace_mode': 'read_only'}}}
    response = asyncio.run(endpoint.handle_line(json.dumps([message])))
    assert isinstance(response, dict) and response['error']['code'] == -32600
    assert not manager.jobs and not executor.started


@pytest.mark.parametrize('framing', ['missing', None, True, [], {}])
def test_hub_http_framing_never_reaches_domain_handler(framing):
    from patchbay.hub.server_v2 import create_hub_v2_server
    from test_hub_server_v2 import StatefulHubV2App
    handler = StatefulHubV2App()
    app = create_hub_v2_server({'auth': {'enabled': False}}, hub_app=handler)
    client = TestClient(app)
    initialized = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    message = {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {
        'name': 'patchbay_work_group_create', 'arguments': {'title': 'Must not create',
        'goal': 'Framing fixture', 'idempotency_key': 'framing'}}}
    if framing != 'missing':
        message['id'] = framing
    response = client.post('/mcp', json=message, headers={'Mcp-Session-Id': initialized.headers['Mcp-Session-Id']})
    if framing == 'missing':
        assert response.status_code == 202
    else:
        assert response.json()['error']['code'] == -32600
    assert not handler.contexts


def test_real_stdio_process_ignores_notifications_and_rejects_batches_before_durable_work(tmp_path):
    config = make_config(tmp_path)
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    launcher = '''import asyncio,sys
import patchbay.stdio as stdio
class NoInferenceExecutor:
    def __init__(self, config, manager): self.manager=manager
    def schedule_job(self, job_id): raise RuntimeError("Unexpected inference admission in framing fixture")
stdio.JobExecutor=NoInferenceExecutor
raise SystemExit(asyncio.run(stdio.run_stdio(sys.argv[1])))
'''
    call = {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'codex_worker_start',
        'arguments': {'name': 'No worker', 'brief': 'Framing only', 'workspace_mode': 'read_only'}}}
    messages = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, call, [{**call, 'id': 1}],
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
    env = {key: value for key, value in os.environ.items()
           if key not in {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN'}}
    env.update(PATCHBAY_HOME=str(tmp_path / 'private-runtime'), CODEX_HOME=str(tmp_path / 'no-credentials'))
    completed = subprocess.run([sys.executable, '-c', launcher, str(config_path)],
        input='\n'.join(json.dumps(message) for message in messages)+'\n', env=env,
        text=True, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    replies = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(replies) == 3
    assert replies[0]['id'] == 0 and replies[-1]['id'] == 2
    assert replies[1]['error']['code'] == -32600
    assert not list((tmp_path / 'logs/jobs/state').glob('*.json'))

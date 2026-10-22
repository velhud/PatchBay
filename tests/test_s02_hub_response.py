"""Trusted Hub admission survives an absent/invalid presentation result."""
import asyncio

from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.protocol_v2 import HubProtocolV2
from patchbay.hub.operations import public_envelope


def test_hub_exception_after_commit_returns_existing_admission_without_output_id(tmp_path):
    store = HubStoreV2(tmp_path / 'hub.sqlite3')
    class Handler:
        fail = True
        def __init__(self): self.store = store
        async def handle_tool_call(self, name, arguments, *, context=None):
            operation = store.create_operation(tool=name, logical_target='fixture',
                idempotency_key=arguments['idempotency_key'], payload=arguments)
            if self.fail:
                raise RuntimeError('private-exception-must-not-escape')
            return public_envelope('pending', operation={'operation_id': operation['operation_id'], 'state': operation['state']})
    handler = Handler()
    protocol = HubProtocolV2(handler)
    arguments = {'title': 'Fixture', 'goal': 'Committed admission', 'idempotency_key': 'same-intent'}
    message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'patchbay_work_group_create', 'arguments': arguments}}
    response = asyncio.run(protocol.handle_message(message))
    data = response['error']['data']
    assert data['effect_may_exist'] and not data['success_asserted']
    operation = store.get_operation(data['operation_id'])
    assert operation and operation['idempotency_key'] == arguments['idempotency_key']
    assert 'private-exception-must-not-escape' not in str(data)
    handler.fail = False
    replay = asyncio.run(HubProtocolV2(handler).handle_message(message))
    assert replay['result']['structuredContent']['operation']['operation_id'] == operation['operation_id']
    assert store._connection.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 1
    store.close()

"""Pure MCP request framing shared by independent workflow entrypoints."""
from collections.abc import Mapping


def request_identity(message):
    """Validate before dispatch. MCP IDs are strings/integers, never null/bool.

    ID-less messages retain notification semantics and never call domain tools.
    """
    if not isinstance(message, Mapping) or message.get('jsonrpc') != '2.0':
        raise ValueError('Expected a JSON-RPC 2.0 request object')
    if not isinstance(message.get('method'), str) or not message['method']:
        raise ValueError('Expected a non-empty JSON-RPC method')
    if 'id' not in message:
        return False, None
    value = message['id']
    if type(value) not in (str, int):
        raise ValueError('MCP request ID must be a string or integer')
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeError as error:
            raise ValueError('MCP request ID must be valid UTF-8') from error
    return True, value


def invalid_request(message):
    return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': message}}

from patchbay.hub.protocol_v2 import HUB_V2_INSTRUCTIONS
from patchbay.protocol.mcp import SERVER_INSTRUCTIONS
from patchbay.workers.model_options import MODEL_SELECTION_GUIDANCE


def test_manager_surfaces_share_one_explicit_luna_policy():
    for instructions in (SERVER_INSTRUCTIONS, HUB_V2_INSTRUCTIONS):
        assert "gpt-6-luna" in instructions
        assert "reasoning_effort='high'" in instructions
    assert "do not follow either rule automatically" in SERVER_INSTRUCTIONS
    assert "never route to them silently" in HUB_V2_INSTRUCTIONS
    assert MODEL_SELECTION_GUIDANCE["effective_default"]["model_id"] == "gpt-6-luna"
    assert MODEL_SELECTION_GUIDANCE["effective_default"]["reasoning_effort"] == "high"
    assert "Never hide model routing" in MODEL_SELECTION_GUIDANCE["manager_rule"]

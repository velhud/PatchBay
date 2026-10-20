import json
import stat

import pytest

from scripts.register_desktop_task import register


OLD_THREAD = "00000000-0000-7000-8000-000000000011"
NEW_THREAD = "00000000-0000-7000-8000-000000000012"


def _write_targets(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_register_remaps_existing_alias_atomically_and_preserves_private_options(tmp_path):
    path = tmp_path / "targets.json"
    _write_targets(
        path,
        {
            "targets": {
                "MTP Luna": {
                    "thread_id": OLD_THREAD,
                    "output_format": "markdown",
                    "sandbox": "workspace-write",
                }
            }
        },
    )

    register(str(path), "MTP Luna", NEW_THREAD)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["targets"]["MTP Luna"] == {
        "thread_id": NEW_THREAD,
        "output_format": "markdown",
        "sandbox": "workspace-write",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_register_rejects_duplicate_task_ids_and_non_private_files(tmp_path):
    path = tmp_path / "targets.json"
    _write_targets(
        path,
        {"targets": {"One": {"thread_id": OLD_THREAD}, "Two": {"thread_id": NEW_THREAD}}},
    )
    with pytest.raises(ValueError, match="already registered"):
        register(str(path), "One", NEW_THREAD)

    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        register(str(path), "One", OLD_THREAD)

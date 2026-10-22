"""Passive fixture distributions; never invoke the fake or installed CLI."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from patchbay.native_profile import check_installation, launch_profile, receipt_status


def profile(tmp_path):
    root = tmp_path/'distribution'
    (root/'bin').mkdir(parents=True)
    binary = root/'bin/codex'
    binary.write_text('#!/bin/sh\nprintf version-only\n')
    binary.chmod(0o755)
    companion = root/'required-host'
    companion.write_text('fixture companion')
    manifest = dict(format='patchbay-native-distribution-v1', version='fixture-version',
        provenance='disposable fixture manifest; not an official distribution', files=[
            dict(path=str(f.relative_to(root)), sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                 role='launcher' if f==binary else 'companion', executable=f==binary)
            for f in [binary,companion]])
    return root,binary,companion,manifest


def test_manifest_checks_companions_and_receipt_becomes_stale(tmp_path):
    root,binary,companion,manifest = profile(tmp_path)
    result = check_installation(root,manifest,executable=binary)
    assert result['status']=='matched_supplied_manifest'
    assert not result['native_execution_qualified'] and not result['client_qualified']
    receipt = tmp_path/'receipt.json'
    receipt.write_text(json.dumps(result))
    assert receipt_status(receipt,binary)['status']=='current_static_binding'
    companion.unlink()
    assert receipt_status(receipt,binary)['status']=='stale_or_unavailable'
    assert check_installation(root,manifest,executable=binary)['status']=='failed'
    assert binary.exists()  # PATH/version-only presence did not prove companions.


@pytest.mark.parametrize('name',['../escape','/absolute','bin/codex/../../escape','bad\\path'])
def test_unsafe_distribution_paths_are_refused(tmp_path,name):
    root,binary,_,manifest=profile(tmp_path)
    manifest['files']=[dict(path=name,sha256='0'*64)]
    with pytest.raises(ValueError):check_installation(root,manifest,executable=binary)


def test_launch_pins_launcher_without_conflating_requested_and_observed(tmp_path):
    root,binary,_,_=profile(tmp_path)
    home=tmp_path/'home';home.mkdir()
    workspace=tmp_path/'workspace';workspace.mkdir()
    schema=tmp_path/'schema.json';schema.write_text('{}')
    env={'PATH':str(root/'bin'),'CODEX_HOME':str(home),'HOME':str(tmp_path)}
    command=['codex','exec','--model','gpt-6-luna','-c','model_reasoning_effort="high"','-']
    actual,before=launch_profile(command,environment=env,cwd=workspace,config={},schema=schema)
    assert actual[0]==str(binary)
    assert before['requested_model']=='gpt-6-luna' and before['requested_reasoning_effort']=='high'
    assert before['observed_model'] is None and before['observed_session_id'] is None
    assert not before['native_execution_qualified']
    assert str(home) not in json.dumps(before) and str(workspace) not in json.dumps(before)
    (home/'config.toml').write_text('model = "fixture"')
    _,changed=launch_profile(command,environment=env,cwd=workspace,config={},schema=schema)
    assert before['configuration_sha256']!=changed['configuration_sha256']
    env['CODEX_HOME']=str(tmp_path/'other-home')
    _,other=launch_profile(command,environment=env,cwd=workspace,config={},schema=schema)
    assert other['home_id']!=before['home_id']


def test_connector_ready_is_independent_of_native_installation(tmp_path):
    from patchbay.connector.status import connector_status
    result=connector_status({'repositories':{'allowed':[str(tmp_path)]}},environ={'PATH':''})
    assert result['ready'] and result['qualification']['connector_ready']
    assert not result['qualification']['native_execution_qualified']


def test_effective_cd_is_part_of_continuation_binding(tmp_path):
    root,binary,_,_=profile(tmp_path)
    schema=tmp_path/'schema.json';schema.write_text('{}')
    env={'PATH':str(root/'bin'),'CODEX_HOME':str(tmp_path/'home')}
    _,initial=launch_profile([str(binary),'exec'],environment=env,cwd=tmp_path,config={},schema=schema)
    _,changed=launch_profile([str(binary),'exec','--cd','elsewhere'],environment=env,cwd=tmp_path,config={},schema=schema)
    assert initial['workspace_id']!=changed['workspace_id']
    assert initial['process_cwd_id']==changed['process_cwd_id']

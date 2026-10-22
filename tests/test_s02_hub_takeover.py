"""Delayed controller calls cannot undo an intervening explicit takeover."""
from patchbay.hub.runtime_v2 import WORK_GROUP_ENTITY
from test_hub_runtime_v2 import make_runtime, enroll_online, create_group, context


def test_resume_rechecks_owner_after_admission_before_group_effect(tmp_path, monkeypatch):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id='machine_fixture')
    first, second = context('first'), context('second')
    group_id = create_group(runtime, caller=first)['result']['work_group']['work_group_id']
    original = runtime._finish_group_resume
    intervened = []
    def paused(**values):
        if values['identity'].participant_ref == 'conversation_first' and not intervened:
            intervened.append(True)
            successor = runtime.resume_work_group(work_group_id=group_id, idempotency_key='takeover',
                takeover=True, takeover_reason='Explicit successor', context=second)
            assert successor['status'] == 'ok'
        return original(**values)
    monkeypatch.setattr(runtime, '_finish_group_resume', paused)
    delayed = runtime.resume_work_group(work_group_id=group_id, idempotency_key='delayed-old-resume', context=first)
    assert delayed['status'] == 'blocked'
    assert store.get_entity(WORK_GROUP_ENTITY, group_id)['record']['active_participant_ref'] == 'conversation_second'
    successor = runtime.resume_work_group(work_group_id=group_id, idempotency_key='successor-progress', context=second)
    assert successor['status'] == 'ok'
    store.close()

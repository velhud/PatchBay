"""Recording-only discovery-to-signal race; never signal a fabricated PID."""
import signal
from types import SimpleNamespace

from patchbay.jobs import process_supervisor as supervisor


def test_owned_discovery_preserves_identity_until_actual_signal(monkeypatch):
    identity = ['owned-start']
    calls = []
    tracker = SimpleNamespace(poll=lambda _: None, tracked={43210: 'owned-start'},
        job_marker='', mark_uncertain=lambda _: None)
    monkeypatch.setattr(supervisor, '_descendants', lambda _: set())
    monkeypatch.setattr(supervisor, '_marker_pids', lambda _: set())
    monkeypatch.setattr(supervisor, '_process_identity', lambda _: identity[0])
    monkeypatch.setattr(supervisor, '_pid_live', lambda _: True)
    monkeypatch.setattr(supervisor, 'os', SimpleNamespace(kill=lambda *args: calls.append(args)))
    owned = supervisor._owned_pids(99999, tracker)
    identity[0] = 'replacement-start'
    supervisor._signal_pids(owned, signal.SIGTERM)
    assert calls == []


def test_unknown_live_identity_remains_fenced(monkeypatch):
    reasons=[]
    tracker=SimpleNamespace(poll=lambda _:None,tracked={43210:'owned'},job_marker='',mark_uncertain=reasons.append)
    monkeypatch.setattr(supervisor,'_descendants',lambda _:set())
    monkeypatch.setattr(supervisor,'_marker_pids',lambda _:set())
    monkeypatch.setattr(supervisor,'_process_identity',lambda _:'')
    monkeypatch.setattr(supervisor,'_pid_live',lambda _:True)
    assert not supervisor._owned_pids(99999,tracker)
    assert reasons==['owned_process_identity_unavailable']


def test_pidfd_is_rechecked_and_closed_without_signalling_replacement(monkeypatch):
    calls=[]
    identities=iter(['owned','replacement'])
    monkeypatch.setattr(supervisor.sys,'platform','linux')
    monkeypatch.setattr(supervisor,'_pid_live',lambda _:True)
    monkeypatch.setattr(supervisor,'_process_identity',lambda _:next(identities))
    monkeypatch.setattr(supervisor,'os',SimpleNamespace(pidfd_open=lambda *_:17,
        close=lambda fd:calls.append(('close',fd)),kill=lambda *a:calls.append(('kill',a))))
    monkeypatch.setattr(supervisor.signal,'pidfd_send_signal',lambda *a:calls.append(('signal',a)),raising=False)
    assert not supervisor._signal_pids([supervisor.OwnedProcessReference(43210,'owned','tracked')],signal.SIGTERM)
    assert calls==[('close',17)]


def test_pidfd_signal_uses_handle_not_numeric_pid(monkeypatch):
    calls=[]
    monkeypatch.setattr(supervisor.sys,'platform','linux')
    monkeypatch.setattr(supervisor,'_pid_live',lambda _:True)
    monkeypatch.setattr(supervisor,'_process_identity',lambda _:'owned')
    monkeypatch.setattr(supervisor,'os',SimpleNamespace(pidfd_open=lambda *_:17,
        close=lambda fd:calls.append(('close',fd)),kill=lambda *a:calls.append(('kill',a))))
    monkeypatch.setattr(supervisor.signal,'pidfd_send_signal',lambda *a:calls.append(('signal',a)),raising=False)
    assert supervisor._signal_pids([supervisor.OwnedProcessReference(43210,'owned','tracked')],signal.SIGTERM)
    assert calls==[('signal',(17,signal.SIGTERM,None,0)),('close',17)]


def test_darwin_zombie_without_bsd_info_is_not_unknown_live(monkeypatch):
    monkeypatch.setattr(supervisor.sys,'platform','darwin')
    monkeypatch.setattr(supervisor,'_darwin_process_info',lambda _:None)
    monkeypatch.setattr(supervisor.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='Z\n'))
    monkeypatch.setattr(supervisor,'os',SimpleNamespace(kill=lambda *_:None))
    assert not supervisor._pid_live(43210)


def test_darwin_unavailable_discovery_is_not_exit_proof(monkeypatch):
    monkeypatch.setattr(supervisor.sys,'platform','darwin')
    monkeypatch.setattr(supervisor,'_darwin_process_info',lambda _:None)
    def unavailable(*a,**k):raise OSError('fixture')
    monkeypatch.setattr(supervisor.subprocess,'run',unavailable)
    monkeypatch.setattr(supervisor,'os',SimpleNamespace(kill=lambda *_:None))
    assert supervisor._pid_live(43210)


def test_failed_darwin_status_query_cannot_prove_exit(monkeypatch):
    monkeypatch.setattr(supervisor.sys,'platform','darwin')
    monkeypatch.setattr(supervisor,'_darwin_process_info',lambda _:None)
    monkeypatch.setattr(supervisor.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='query failed'))
    monkeypatch.setattr(supervisor,'os',SimpleNamespace(kill=lambda *_:None))
    assert supervisor._pid_live(43210)

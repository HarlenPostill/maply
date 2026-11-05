"""Cloud queue lifecycle, with no desktop, network, model calls or database."""
import importlib.util
from pathlib import Path
import uuid
from types import SimpleNamespace
import json
import pytest


def module():
    path = Path(__file__).parents[2] / 'scripts/cloud/supervisor.py'
    spec = importlib.util.spec_from_file_location('cloud_supervisor', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_boot_pauses_running_runs_instead_of_spending(monkeypatch):
    supervisor = module()
    run = str(uuid.uuid4())
    actions, handlers = [], {}
    monkeypatch.setattr(supervisor, 'queue', lambda: [run])
    monkeypatch.setattr(supervisor, 'pause', lambda *args: actions.append(args))
    monkeypatch.setattr(supervisor.signal, 'signal', lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(supervisor.time, 'sleep', lambda _: handlers[supervisor.signal.SIGTERM]())
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *_: (_ for _ in ()).throw(AssertionError('Must wait for user resume')))
    supervisor.main()
    assert actions == [(run, 'Cloud capture service restarted. Resume this run in Maply to continue.')]


@pytest.mark.parametrize('exit_code', [1, 124])
def test_failed_worker_pauses_once_and_does_not_restart(monkeypatch, exit_code):
    supervisor = module()
    run = str(uuid.uuid4())
    state = {'status': 'running', 'ticks': 0}
    actions, handlers = [], {}
    monkeypatch.setattr(supervisor, 'queue', lambda: [run] if state['ticks'] and state['status'] == 'running' else [])
    monkeypatch.setattr(supervisor, 'api', lambda *_: {'status': state['status']})
    def pause(*args):
        state['status'] = 'paused'
        actions.append(('pause', args[0]))
    def start(args):
        actions.append(('start', args[-1]))
        return SimpleNamespace(poll=lambda: exit_code)
    def tick(_):
        state['ticks'] += 1
        if state['ticks'] >= 7:
            handlers[supervisor.signal.SIGTERM]()
    monkeypatch.setattr(supervisor, 'pause', pause)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', start)
    monkeypatch.setattr(supervisor.signal, 'signal', lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(supervisor.time, 'sleep', tick)
    supervisor.main()
    assert actions == [('start', run), ('pause', run)]


def test_watchdog_timeout_restarts_only_failed_yolo_slot_after_backoff(monkeypatch):
    supervisor = module()
    run = str(uuid.uuid4())
    state = {'ticks': 0}
    actions, handlers = [], {}
    monkeypatch.setattr(supervisor, 'desktops', lambda: ['local', '192.0.2.1'])
    monkeypatch.setattr(supervisor, 'queue', lambda: [run] if state['ticks'] else [])
    monkeypatch.setattr(supervisor, 'api', lambda *_: {'status': 'running'})
    # Conditional pauses record the error but leave a running YOLO run active.
    monkeypatch.setattr(supervisor, 'pause', lambda *args: actions.append(('pause', state['ticks'])))
    monkeypatch.setattr(supervisor.time, 'monotonic', lambda: state['ticks'] * 10)

    def launch(host, rid):
        first_remote = host == '192.0.2.1' and not any(action[:2] == ('start', host) for action in actions)
        actions.append(('start', host, state['ticks']))
        child = SimpleNamespace(code=None)
        child.poll = lambda: 124 if first_remote and state['ticks'] >= 4 else child.code
        child.terminate = lambda: setattr(child, 'code', 0)
        child.wait = lambda **_: 0
        return child

    def tick(_):
        state['ticks'] += 1
        if state['ticks'] >= 13:
            handlers[supervisor.signal.SIGTERM]()

    monkeypatch.setattr(supervisor, 'launch', launch)
    monkeypatch.setattr(supervisor.signal, 'signal', lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(supervisor.time, 'sleep', tick)
    supervisor.main()
    starts = [action for action in actions if action[0] == 'start']
    assert [action[1] for action in starts] == ['local', '192.0.2.1', '192.0.2.1']
    pauses = [action for action in actions if action[0] == 'pause']
    assert len(pauses) == 1
    assert starts[-1][2] - pauses[0][1] >= 3


def test_fleet_reloads_and_rejects_duplicate_desktops(monkeypatch, tmp_path):
    supervisor = module()
    inventory = tmp_path / 'fleet.json'
    monkeypatch.setattr(supervisor, 'FLEET', inventory)
    assert supervisor.desktops() == ['local']
    inventory.write_text(json.dumps(['192.0.2.1']))
    assert supervisor.desktops() == ['local', '192.0.2.1']
    inventory.write_text(json.dumps(['192.0.2.1', '192.0.2.1']))
    with pytest.raises(ValueError, match='only once'):
        supervisor.desktops()
    inventory.write_text(json.dumps(['-oProxyCommand=bad']))
    with pytest.raises(ValueError):
        supervisor.desktops()


def test_multiple_desktops_share_run_and_user_pause_stops_every_worker(monkeypatch):
    supervisor = module()
    run = str(uuid.uuid4())
    state = {'ticks': 0, 'paused': False}
    actions, handlers = [], {}
    monkeypatch.setattr(supervisor, 'desktops', lambda: ['local', '192.0.2.1', '192.0.2.2'])
    monkeypatch.setattr(supervisor, 'queue', lambda: [run] if state['ticks'] and not state['paused'] else [])
    def launch(host, rid):
        actions.append(('start', host, rid))
        return SimpleNamespace(poll=lambda: None, terminate=lambda: actions.append(('stop', host)), wait=lambda **_: 0)
    def tick(_):
        state['ticks'] += 1
        if state['ticks'] == 5:
            state['paused'] = True
        if state['ticks'] == 9:
            handlers[supervisor.signal.SIGTERM]()
    monkeypatch.setattr(supervisor, 'launch', launch)
    monkeypatch.setattr(supervisor.signal, 'signal', lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(supervisor.time, 'sleep', tick)
    supervisor.main()
    assert actions == [('start', host, run) for host in ['local', '192.0.2.1', '192.0.2.2']] + [
        ('stop', host) for host in ['local', '192.0.2.1', '192.0.2.2']]


def test_removing_vm_drains_only_that_desktop(monkeypatch):
    supervisor = module()
    run = str(uuid.uuid4())
    state = {'ticks': 0}
    actions, handlers = [], {}
    monkeypatch.setattr(supervisor, 'desktops', lambda: ['local', '192.0.2.1'] if state['ticks'] < 5 else ['local'])
    monkeypatch.setattr(supervisor, 'queue', lambda: [run] if state['ticks'] else [])
    def launch(host, rid):
        child = SimpleNamespace(code=None)
        child.poll = lambda: child.code
        def terminate():
            actions.append(('stop', host))
            child.code = 0
        child.terminate, child.wait = terminate, lambda **_: 0
        actions.append(('start', host))
        return child
    def tick(_):
        state['ticks'] += 1
        if state['ticks'] == 9:
            handlers[supervisor.signal.SIGTERM]()
    monkeypatch.setattr(supervisor, 'launch', launch)
    monkeypatch.setattr(supervisor.signal, 'signal', lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(supervisor.time, 'sleep', tick)
    supervisor.main()
    assert actions == [('start', 'local'), ('start', '192.0.2.1'), ('stop', '192.0.2.1'), ('stop', 'local')]


def test_two_slots_on_same_vm_have_separate_accounts_and_tunnels(monkeypatch, tmp_path):
    supervisor = module()
    inventory = tmp_path / 'fleet.json'
    monkeypatch.setattr(supervisor, 'FLEET', inventory)
    inventory.write_text(json.dumps(['192.0.2.1', {'host': '192.0.2.1', 'slot': 2}]))
    assert supervisor.desktops() == ['local', '192.0.2.1', '192.0.2.1/2']
    calls = []
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda args, **kwargs: calls.append(args))
    run = str(uuid.uuid4())
    for host in supervisor.desktops()[1:]:
        supervisor.launch(host, run)
    assert calls[0][-2:] == ['maply@192.0.2.1', run]
    assert calls[1][-2:] == ['maply2@192.0.2.1', run]
    assert calls[0][calls[0].index('-R') + 1] == '127.0.0.1:18000:127.0.0.1:8000'
    assert calls[1][calls[1].index('-R') + 1] == '127.0.0.1:18001:127.0.0.1:8000'
    inventory.write_text(json.dumps(['192.0.2.1', {'host': '192.0.2.1', 'slot': 1}]))
    with pytest.raises(ValueError, match='only once'):
        supervisor.desktops()
    inventory.write_text(json.dumps([{'host': '192.0.2.1', 'slot': 3}]))
    with pytest.raises(ValueError):
        supervisor.desktops()

"""Exercise SSH EOF cleanup against real subprocesses, without a desktop."""
import os
from pathlib import Path
import subprocess
import sys


def test_ssh_disconnect_terminates_worker(tmp_path):
    module = Path(__file__).parents[2] / 'scripts/cloud/remote_worker.py'
    result = tmp_path / 'stopped'
    worker = tmp_path / 'worker.py'
    worker.write_text('''import signal, sys, time
from pathlib import Path
def stop(*_):
    Path(sys.argv[1]).write_text('stopped')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print('ready', flush=True)
while True:
    time.sleep(.1)
''')
    code = f"import runpy; m=runpy.run_path({str(module)!r}); m['supervise']({[sys.executable, str(worker), str(result)]!r})"
    process = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([process.stdout], [], [], 10)[0], 'worker did not start'
        assert process.stdout.readline().strip() == 'ready'
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert result.read_text() == 'stopped'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_forced_command_rejects_shell_commands():
    module = Path(__file__).parents[2] / 'scripts/cloud/remote_worker.py'
    result = subprocess.run([sys.executable, str(module)], env={**os.environ, 'SSH_ORIGINAL_COMMAND': 'id; echo bad'}, capture_output=True)
    assert result.returncode != 0
    assert b'badly formed' in result.stderr


def test_second_account_uses_its_own_display_home_and_api(monkeypatch):
    import runpy
    from types import SimpleNamespace
    import uuid
    module = Path(__file__).parents[2] / 'scripts/cloud/remote_worker.py'
    values = runpy.run_path(str(module))
    main = values['main']
    run = str(uuid.uuid4())
    monkeypatch.setenv('SSH_ORIGINAL_COMMAND', run)
    monkeypatch.setenv('HOME', '/home/maply')
    monkeypatch.setenv('DISPLAY', ':99')
    monkeypatch.setenv('XAUTHORITY', '/home/maply/.Xauthority')
    monkeypatch.setattr(main.__globals__['pwd'], 'getpwuid', lambda uid: SimpleNamespace(pw_name='maply2', pw_dir='/home/maply2'))
    calls = []
    monkeypatch.setitem(main.__globals__, 'supervise', lambda args: calls.append(args) or 0)
    assert main() == 0
    assert os.environ['HOME'] == '/home/maply2'
    assert os.environ['DISPLAY'] == ':100'
    assert os.environ['XAUTHORITY'] == '/home/maply2/.Xauthority'
    assert calls[0][-4:] == ['--server', 'http://127.0.0.1:18001', '--run', run]

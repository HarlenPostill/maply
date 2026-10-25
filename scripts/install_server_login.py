"""Install the permanent Mac server's user-level startup job. Run on the server Mac only."""
import os
from pathlib import Path
import plistlib
import subprocess

root = Path(__file__).resolve().parents[1]
script = root / 'scripts/start_server.sh'
if not script.is_file():
    raise SystemExit('Server startup script is missing')
logs = root / 'data/server-logs'
logs.mkdir(parents=True, exist_ok=True)
folder = Path.home() / 'Library/LaunchAgents'
folder.mkdir(parents=True, exist_ok=True)
path = folder / 'io.maply.server.plist'
config = {'Label': 'io.maply.server', 'ProgramArguments': ['/bin/zsh', str(script)],
          'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
          'WorkingDirectory': str(root), 'StandardOutPath': str(logs / 'startup.log'),
          'StandardErrorPath': str(logs / 'startup-error.log')}
with path.open('wb') as output:
    plistlib.dump(config, output)
os.chmod(path, 0o600)
subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/io.maply.server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
print('Maply server will start after this user logs in. The startup job also prevents idle system sleep.')

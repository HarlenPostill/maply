"""Install an explicitly requested historical tunnel; local development needs none."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
from connect_server import ssh_arguments

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config', type=Path, required=True)
args = parser.parse_args()
config = json.loads(args.config.read_text())
arguments = ssh_arguments(config)
logs = root / 'data/client-logs'
logs.mkdir(parents=True, exist_ok=True)
folder = Path.home() / 'Library/LaunchAgents'
folder.mkdir(parents=True, exist_ok=True)
path = folder / 'io.maply.client-tunnel.plist'
settings = {'Label': 'io.maply.client-tunnel', 'ProgramArguments': ['/usr/bin/ssh', *arguments[1:]],
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
            'StandardOutPath': str(logs / 'tunnel.log'), 'StandardErrorPath': str(logs / 'tunnel-error.log')}
with path.open('wb') as output:
    plistlib.dump(settings, output)
os.chmod(path, 0o600)
subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/io.maply.client-tunnel'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
print('Private Maply tunnel installed. It reconnects automatically while the server is reachable.')

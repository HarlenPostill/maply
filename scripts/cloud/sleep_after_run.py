"""One-shot root timer: power off enrolled temporary VMs after a run completes.

The main OVH server stays online. DigitalOcean billing continues while off.
"""
import argparse
import json
import subprocess
import urllib.request
from pathlib import Path

FLEET = Path('/etc/maply/capture-fleet.json')
STATE = Path('/var/lib/maply/sleep-after-run.json')


def ready(runs, target):
    return (any(r['id'] == target and r['status'] == 'completed' for r in runs)
            and not any(r['status'] == 'running' for r in runs))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    state = json.loads(STATE.read_text())
    with urllib.request.urlopen('http://127.0.0.1:8000/api/capture/runs', timeout=60) as response:
        runs = json.load(response)['runs']
    eligible = ready(runs, state['run_id'])
    print(json.dumps({'ready': eligible, 'hosts': state['hosts'], 'done': state.get('done', [])}), flush=True)
    if args.check or not eligible:
        return
    # Remove only these hosts so the supervisor cannot relaunch their workers.
    fleet = json.loads(FLEET.read_text())
    keep = [entry for entry in fleet if (entry['host'] if isinstance(entry, dict) else entry) not in state['hosts']]
    temp = FLEET.with_suffix('.sleep.tmp')
    temp.write_text(json.dumps(keep) + '\n')
    temp.replace(FLEET)
    for host in state['hosts']:
        if host in state.get('done', []):
            continue
        subprocess.run(['ssh', '-i', '/home/maply/.ssh/capture_ed25519',
                        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=yes',
                        '-o', 'UserKnownHostsFile=/home/maply/.ssh/known_hosts',
                        '-o', 'ConnectTimeout=15', 'root@' + host, 'poweroff'], check=True)
        state.setdefault('done', []).append(host)
        temp = STATE.with_suffix('.tmp')
        temp.write_text(json.dumps(state) + '\n')
        temp.replace(STATE)
    subprocess.run(['systemctl', 'disable', '--now', 'maply-sleep-after-run.timer'], check=True)


if __name__ == '__main__':
    main()

"""Open a private SSH connection to the permanent Maply server, without starting a local app."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def ssh_arguments(config):
    host = config['host']
    if not host or host.startswith('-') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@:-' for c in host):
        raise ValueError('Invalid SSH destination')
    local_port, remote_port = int(config.get('local_port', 8000)), int(config.get('remote_port', 8000))
    if not 1 <= local_port <= 65535 or not 1 <= remote_port <= 65535:
        raise ValueError('Invalid tunnel port')
    return ['ssh', '-N', '-T', '-i', str(Path(config['identity_file']).expanduser()),
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-L', f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}', host]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'data/client-server.json')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    port = int(config.get('local_port', 8000))
    # An existing local app must never be mistaken for the authoritative server.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', port)) == 0:
            raise SystemExit(f'Port {port} is already in use. If the Maply tunnel service is running, open http://127.0.0.1:{port}. Otherwise stop the local Maply app first.')
    process = subprocess.Popen(ssh_arguments(config))
    try:
        url = f'http://127.0.0.1:{port}'
        for _ in range(30):
            if process.poll() is not None:
                raise SystemExit('SSH connection failed; check the server address, network and SSH key.')
            try:
                with urllib.request.urlopen(url + '/api/jobs', timeout=2) as response:
                    jobs = json.load(response)
                expected = set(config.get('expected_job_ids', []))
                if not expected.issubset({j['id'] for j in jobs}):
                    raise SystemExit('The connected server does not contain the expected Maply jobs.')
                print(f'Connected to {config["host"]}. Open {url}; the extension uses the same address.', flush=True)
                if not args.no_open:
                    webbrowser.open(url)
                break
            except OSError:
                time.sleep(1)
        else:
            raise SystemExit('The SSH tunnel opened but the server app is not ready.')
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        process.terminate()
        process.wait()


if __name__ == '__main__':
    main()

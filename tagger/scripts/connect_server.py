"""Open an explicitly configured historical SSH tunnel on a separate local port."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import webbrowser

def ssh_arguments(config):
    host = config['host']
    if not host or host.startswith('-') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@:-' for c in host):
        raise ValueError('Invalid SSH destination')
    local_port, remote_port = int(config.get('local_port', 18000)), int(config.get('remote_port', 8000))
    if not 1 <= local_port <= 65535 or not 1 <= remote_port <= 65535:
        raise ValueError('Invalid tunnel port')
    if local_port in {8000, 5173}:
        raise ValueError(f'Port {local_port} is reserved for local development; use a separate tunnel port such as 18000.')
    arguments = ['ssh', '-N', '-T', '-i', str(Path(config['identity_file']).expanduser()),
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-L', f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}']
    if config.get('desktop_local_port') is not None:
        desktop_port = int(config['desktop_local_port'])
        if not 1 <= desktop_port <= 65535 or desktop_port == local_port:
            raise ValueError('Invalid desktop tunnel port')
        if desktop_port in {8000, 5173}:
            raise ValueError(f'Port {desktop_port} is reserved for local development.')
        arguments += ['-L', f'127.0.0.1:{desktop_port}:127.0.0.1:5900']
    return [*arguments, host]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    arguments = ssh_arguments(config)
    port = int(config.get('local_port', 18000))
    # An existing local app must never be mistaken for the authoritative server.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', port)) == 0:
            raise SystemExit(f'Port {port} is already in use. Choose another tunnel port.')
    process = subprocess.Popen(arguments)
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

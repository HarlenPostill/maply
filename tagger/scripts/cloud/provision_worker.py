"""Install a capture-only VM and enroll a restricted coordinator SSH key.

Usage: python3 scripts/cloud/provision_worker.py IP [--skip-packages]
Does not add the VM to the active fleet or start paid capture.
"""
import argparse
import ipaddress
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('address', type=ipaddress.IPv4Address)
    parser.add_argument('--coordinator', default='maply-cloud')
    parser.add_argument('--key', default=str(Path.home() / '.ssh/maply_cloud_ed25519'))
    parser.add_argument('--skip-packages', action='store_true')
    args = parser.parse_args()
    address = str(args.address)
    ssh = ['ssh', '-i', args.key, '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=15', f'root@{address}']
    coordinator = ['ssh', args.coordinator]
    for attempt in range(24):
        ready = subprocess.run(ssh + ['true'], capture_output=True)
        if ready.returncode == 0:
            break
        if attempt == 23:
            raise RuntimeError(f'SSH is not ready on {address}: {ready.stderr.decode()[:500]}')
        time.sleep(5)
    run(ssh + ['cloud-init status --wait'])
    with tempfile.TemporaryDirectory(prefix='maply-worker-') as tmp:
        bundle = Path(tmp) / 'source.tgz'
        with tarfile.open(bundle, 'w:gz') as archive:
            for name in ['backend/app', 'backend/pyproject.toml', 'scripts/capture_worker.py', 'scripts/capture_watchdog.py',
                         'scripts/cloud/desktop.sh', 'scripts/cloud/maply-desktop.service',
                         'scripts/cloud/bootstrap_worker.sh', 'scripts/cloud/remote_worker.py']:
                archive.add(ROOT / name, arcname=name,
                            filter=lambda item: None if '__pycache__' in item.name or item.name.endswith('.pyc') else item)
        run(['scp', '-i', args.key, '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', str(bundle), f'root@{address}:/tmp/maply-worker-source.tgz'])
    run(ssh + ['mkdir -p /opt/maply && tar -xzf /tmp/maply-worker-source.tgz -C /opt/maply'])
    if not args.skip_packages:
        run(ssh + ['bash /opt/maply/scripts/cloud/bootstrap_worker.sh > /var/log/maply-bootstrap.log 2>&1'])
    public = run(coordinator + ['sudo cat /home/maply/.ssh/capture_ed25519.pub'], capture_output=True).stdout.strip()
    if not public.startswith('ssh-ed25519 ') or '\n' in public:
        raise ValueError('Expected one coordinator public key')
    authorized = ('restrict,port-forwarding,permitlisten="127.0.0.1:18000",'
                  'command="/opt/maply/backend/.venv/bin/python /opt/maply/scripts/cloud/remote_worker.py" ' + public + '\n')
    run(ssh + ['install -d -m 700 -o maply -g maply /home/maply/.ssh && cat > /home/maply/.ssh/authorized_keys && chown maply:maply /home/maply/.ssh/authorized_keys && chmod 600 /home/maply/.ssh/authorized_keys'], input=authorized)
    run(ssh + ['cat > /etc/ssh/sshd_config.d/60-maply-capture.conf && sshd -t && systemctl reload ssh'],
        input='Match User maply\n    AllowTcpForwarding remote\n    PermitListen 127.0.0.1:18000\n    GatewayPorts no\n    ClientAliveInterval 10\n    ClientAliveCountMax 3\nMatch all\n')
    host_key = run(ssh + ['cat /etc/ssh/ssh_host_ed25519_key.pub'], capture_output=True).stdout.strip().split()
    known = f'{address} {host_key[0]} {host_key[1]}'
    # Obtain the key over the already authenticated admin SSH connection.
    command = ('sudo -u maply sh -c ' + shlex.quote(
        f'touch /home/maply/.ssh/known_hosts; grep -qxF {shlex.quote(known)} /home/maply/.ssh/known_hosts || '
        f'printf "%s\\n" {shlex.quote(known)} >> /home/maply/.ssh/known_hosts'))
    run(coordinator + [command])
    run(ssh + ['chown -R maply:maply /opt/maply && systemctl is-active maply-desktop && ss -ltn'])
    print(f'{address} provisioned. Preflight the desktop before adding it to /etc/maply/capture-fleet.json on OVH.')


if __name__ == '__main__':
    main()

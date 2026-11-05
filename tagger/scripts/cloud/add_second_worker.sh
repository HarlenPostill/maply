#!/usr/bin/env bash
# Install a separate second desktop on an already provisioned capture-only VM.
# No paid work starts until slot 2 is enrolled on the coordinator.
set -euo pipefail
[[ $EUID -eq 0 ]]
id maply2 >/dev/null 2>&1 || useradd --create-home --shell /bin/bash maply2
usermod -a -G maply maply2
install -m 755 /opt/maply/scripts/cloud/desktop.sh /usr/local/bin/maply-desktop
install -d -m 700 -o maply2 -g maply2 /home/maply2/.ssh
sed 's/127.0.0.1:18000/127.0.0.1:18001/g' /home/maply/.ssh/authorized_keys > /home/maply2/.ssh/authorized_keys
chown maply2:maply2 /home/maply2/.ssh/authorized_keys
chmod 600 /home/maply2/.ssh/authorized_keys
cat > /etc/ssh/sshd_config.d/61-maply-capture-second.conf <<'SSH'
Match User maply2
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:18001
    GatewayPorts no
    ClientAliveInterval 10
    ClientAliveCountMax 3
Match all
SSH
sshd -t
systemctl reload ssh
cat > /etc/systemd/system/maply-desktop-second.service <<'UNIT'
[Unit]
Description=Maply second isolated Chrome desktop
After=network-online.target
Wants=network-online.target

[Service]
User=maply2
Group=maply2
Environment=HOME=/home/maply2
Environment=DISPLAY=:100
Environment=XAUTHORITY=/home/maply2/.Xauthority
Environment=MAPLY_VNC_PORT=5901
ExecStart=/usr/bin/dbus-run-session -- /usr/local/bin/maply-desktop
Restart=on-failure
RestartSec=5
KillMode=control-group

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now maply-desktop-second

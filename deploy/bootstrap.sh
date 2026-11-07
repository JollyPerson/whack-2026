#!/usr/bin/env bash
# First-time server setup, run as root on a fresh Ubuntu LTS instance: bootstrap.sh "<CI public key>"
# Safe to re-run: re-running replaces the CI key, so it also rotates it.
set -euo pipefail
pubkey=${1:?usage: bootstrap.sh "<CI public key>"}
export DEBIAN_FRONTEND=noninteractive
apt() { apt-get -o DPkg::Lock::Timeout=300 -qq "$@"; }

cloud-init status --wait >/dev/null 2>&1 || true
apt update
apt install -y apache2-utils ufw curl   # apache2-utils: htpasswd, for the site password hash
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
printf '%s\n' "$pubkey" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

install -d -m 750 -o deploy -g deploy /opt/debate-ref
for f in .env app.env; do
  [ -e "/opt/debate-ref/$f" ] || install -m 600 -o deploy -g deploy /dev/null "/opt/debate-ref/$f"
done

ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443 >/dev/null
ufw --force enable >/dev/null

# Keys only. 00- sorts before cloud-init's drop-in, and the first value sshd reads wins.
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
  > /etc/ssh/sshd_config.d/00-debate-ref.conf
sshd -t
systemctl reload ssh
echo "Server ready"

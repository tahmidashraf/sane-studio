#!/usr/bin/env bash
# One-time preparation of a fresh Ubuntu server on Oracle Cloud: Docker, and a host firewall
# that lets the web ports in. Runs as root; `deploy/oracle.sh setup` sends it over SSH.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get install -y docker.io docker-compose-v2
systemctl enable --now docker

# Oracle's Ubuntu images reject everything except SSH in the host firewall; open 80 and 443.
for port in 443 80; do
  iptables -C INPUT -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null ||
    iptables -I INPUT 1 -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT
done
if command -v netfilter-persistent >/dev/null; then netfilter-persistent save; fi

docker --version
docker compose version
echo "server ready"

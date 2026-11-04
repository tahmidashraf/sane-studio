#!/usr/bin/env bash
# Deploy and control SANE Studio on an Oracle Cloud server from this PC (Git Bash on Windows).
#
#   deploy/oracle.sh setup    one time: install Docker and open the web ports on the server
#   deploy/oracle.sh deploy   upload this folder, build, start, and print the public link
#   deploy/oracle.sh stop     take the site offline (the server keeps running)
#   deploy/oracle.sh start    bring the site back
#   deploy/oracle.sh status   containers and a health check of the public link
#   deploy/oracle.sh logs     last lines of the app and web-server logs
#
# Reads deploy/server.env: SERVER_IP=<public IP>, optional SSH_USER (default ubuntu) and
# SSH_KEY (default ~/.ssh/sane_oracle).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
[ -f "$here/server.env" ] || { echo "Create $here/server.env with SERVER_IP=<public IP>"; exit 1; }
# shellcheck disable=SC1091
source "$here/server.env"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sane_oracle}"
SITE="$(echo "$SERVER_IP" | tr . -).sslip.io"
COMPOSE="cd ~/sane-studio && sudo docker compose -f deploy/docker-compose.yml"

remote() { ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "$SSH_USER@$SERVER_IP" "$@"; }

case "${1:-}" in
  setup)
    remote 'sudo bash -s' < "$here/server-setup.sh"
    ;;
  deploy)
    echo "Uploading $repo ..."
    tar --exclude=./.git --exclude=./tests --exclude=./deploy/server.env --exclude=./deploy/.env \
        --exclude='__pycache__' -czf - -C "$repo" . |
      remote 'rm -rf ~/sane-studio.new && mkdir -p ~/sane-studio.new && tar -xzf - -C ~/sane-studio.new &&
              rm -rf ~/sane-studio && mv ~/sane-studio.new ~/sane-studio'
    remote "echo SITE_ADDRESS=$SITE > ~/sane-studio/deploy/.env && $COMPOSE up -d --build"
    echo
    echo "SANE Studio is starting at: https://$SITE"
    echo "(the HTTPS certificate is issued on the first visit; allow a minute)"
    ;;
  stop)
    remote "$COMPOSE stop"
    echo "Site offline. Bring it back with: deploy/oracle.sh start"
    ;;
  start)
    remote "$COMPOSE start"
    echo "Site online: https://$SITE"
    ;;
  status)
    remote "$COMPOSE ps"
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$SITE/api/health" || true)"
    echo "https://$SITE/api/health -> HTTP $code"
    ;;
  logs)
    remote "$COMPOSE logs --tail 80"
    ;;
  *)
    sed -n '2,13p' "$0"
    exit 1
    ;;
esac

#!/bin/bash
# Deploy gout's web preview to gout.anomata.eu: this branch (web-version) as the gout-web service
# behind nginx, on the server the other anomata apps use. The projects people make live in
# /var/lib/gout-web and stay across deploys; the code goes to /var/www/gout.
#
#   deploy/deploy-web.sh          upload, install, restart, check (and roll back if it does not answer)
#   deploy/deploy-web.sh --tls    the same, then ask Let's Encrypt for https (once DNS points here)
set -euo pipefail

SERVER="user@your-server"
DOMAIN="gout.anomata.eu"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "web-version" ]; then
    echo "ERROR: on branch '$BRANCH'; the web preview deploys from web-version"
    exit 1
fi
if ! git diff --quiet HEAD; then
    echo "ERROR: uncommitted changes; commit them first (the deploy takes the last commit)"
    exit 1
fi
COMMIT=$(git rev-parse --short HEAD)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git archive --format=tar.gz --prefix=gout/ -o "$TMP/gout.tar.gz" HEAD bin gout LICENSE README.md

echo "Deploying gout web preview $COMMIT to $DOMAIN..."
scp -q "$TMP/gout.tar.gz" deploy/gout-web.service deploy/gout.anomata.eu.nginx "$SERVER:/tmp/"

ssh "$SERVER" bash -s -- "$COMMIT" "$DOMAIN" "${1:-}" <<'REMOTE'
set -euo pipefail
COMMIT="$1"; DOMAIN="$2"; TLS="$3"
LIVE=/var/www/gout
STAGING="$LIVE.staging"
ROLLBACK="$LIVE.rollback"

command -v ffmpeg >/dev/null || apt-get install -y ffmpeg
id gout-web >/dev/null 2>&1 || useradd --system --home-dir /var/lib/gout-web --shell /usr/sbin/nologin gout-web
install -d -o gout-web -g gout-web -m 750 /var/lib/gout-web /var/lib/gout-web/projects

rm -rf "$STAGING"
mkdir -p "$STAGING"
tar -xzf /tmp/gout.tar.gz -C "$STAGING" --strip-components=1
echo "$COMMIT" > "$STAGING/DEPLOYED"
chown -R root:root "$STAGING"
chmod -R a+rX,go-w "$STAGING"
rm -f /tmp/gout.tar.gz

install -m 644 /tmp/gout-web.service /etc/systemd/system/gout-web.service
if [ ! -f "/etc/nginx/sites-available/$DOMAIN" ]; then  # certbot edits it later; never overwrite that
    install -m 644 /tmp/gout.anomata.eu.nginx "/etc/nginx/sites-available/$DOMAIN"
    ln -sf "../sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
fi
rm -f /tmp/gout-web.service /tmp/gout.anomata.eu.nginx
systemctl daemon-reload

systemctl stop gout-web 2>/dev/null || true
rm -rf "$ROLLBACK"
[ -d "$LIVE" ] && mv "$LIVE" "$ROLLBACK"
mv "$STAGING" "$LIVE"
systemctl enable --quiet gout-web
systemctl start gout-web

ok=""
for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8321/api/info >/dev/null 2>&1; then ok=1; break; fi
    sleep 0.5
done
if [ -z "$ok" ]; then
    echo "ERROR: gout-web does not answer; rolling back"
    journalctl -u gout-web -n 20 --no-pager || true
    systemctl stop gout-web || true
    if [ -d "$ROLLBACK" ]; then
        rm -rf "$LIVE"
        mv "$ROLLBACK" "$LIVE"
        systemctl start gout-web || true
    fi
    exit 1
fi

nginx -t -q && systemctl reload nginx
if [ "$TLS" = "--tls" ]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect
fi
echo "gout-web $COMMIT is running: $(curl -fsS -H "Host: $DOMAIN" http://127.0.0.1/api/info | head -c 80)..."
REMOTE

echo ""
echo "=== gout web preview deployed: https://$DOMAIN ==="

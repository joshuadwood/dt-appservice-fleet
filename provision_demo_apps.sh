#!/usr/bin/env bash
#
# provision_demo_apps.sh — stand up 4 Linux App Services for the Dynatrace
# OneAgent fleet demo:
#
#   node-express   NODE:22-lts    Express        startup: node server.js
#   node-fastify   NODE:24-lts    Fastify        startup: node server.js
#   py-flask       PYTHON:3.11    Flask          startup: gunicorn ... app:app
#   py-fastapi     PYTHON:3.12    FastAPI        startup: gunicorn -k uvicorn ... main:app
#
# Two runtimes x two frameworks each, all on OneAgent-supported versions.
# Idempotent-ish: re-running re-deploys code. Requires an authenticated `az`.
#
set -euo pipefail

RG="${RG:-dt-oneagent-fleet-demo}"
LOCATION="${LOCATION:-eastus}"
PLAN="${PLAN:-dt-fleet-plan}"
SKU="${SKU:-B1}"
PREFIX="${PREFIX:-dtfleet}"
# Optionally target a specific subscription (id or name). Suffix is derived from
# the active subscription id, so apps in different subs get distinct, non-colliding
# names automatically.
SUBSCRIPTION="${SUBSCRIPTION:-}"
[ -n "$SUBSCRIPTION" ] && az account set -s "$SUBSCRIPTION"
SUFFIX="${SUFFIX:-$(az account show --query id -o tsv | cut -c1-6)}"
# ONLY = space-separated subset of framework keys to provision
# (node-express node-fastify py-flask py-fastapi). Empty = all four.
ONLY="${ONLY:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NTFY_TOPIC="${NTFY_TOPIC:-}"
echo "Active subscription: $(az account show --query name -o tsv)  (suffix=$SUFFIX)"

notify() { [ -n "$NTFY_TOPIC" ] && curl -s -m 15 -H "Title: $1" -d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null || true; }

# name  runtime         appdir           startup-command
APPS=(
  "${PREFIX}-node-express-${SUFFIX}|NODE:22-lts|apps/node-express|node server.js"
  "${PREFIX}-node-fastify-${SUFFIX}|NODE:24-lts|apps/node-fastify|node server.js"
  "${PREFIX}-py-flask-${SUFFIX}|PYTHON:3.11|apps/py-flask|gunicorn --bind=0.0.0.0:8000 app:app"
  "${PREFIX}-py-fastapi-${SUFFIX}|PYTHON:3.12|apps/py-fastapi|gunicorn -w 2 -k uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000 main:app"
)

echo "== Resource group $RG ($LOCATION) =="
az group create -n "$RG" -l "$LOCATION" -o none

echo "== App Service plan $PLAN ($SKU, Linux) =="
az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku "$SKU" -o none

for spec in "${APPS[@]}"; do
  IFS='|' read -r name runtime appdir startup <<< "$spec"
  key="$(basename "$appdir")"            # e.g. node-express
  if [ -n "$ONLY" ] && ! grep -qw "$key" <<< "$ONLY"; then
    echo "  (skip $key — not in ONLY='$ONLY')"
    continue
  fi
  echo
  echo "== $name  [$runtime] =="

  az webapp create -g "$RG" -p "$PLAN" -n "$name" --runtime "$runtime" -o none

  # Oryx build on deploy (npm install / pip install -r requirements.txt)
  az webapp config appsettings set -g "$RG" -n "$name" -o none \
    --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true WEBSITES_CONTAINER_START_TIME_LIMIT=600

  # Explicit startup command — realistic "before" state the fleet tool will
  # later capture as START_APP_CMD during instrumentation.
  az webapp config set -g "$RG" -n "$name" --startup-file "$startup" -o none

  # Enable Kudu basic auth (used for verification/log pulls).
  az resource update -g "$RG" --name scm --namespace Microsoft.Web \
    --resource-type basicPublishingCredentialsPolicies --parent "sites/$name" \
    --set properties.allow=true -o none 2>/dev/null || true

  # Package + deploy code.
  zip="/tmp/${name}.zip"
  ( cd "$HERE/$appdir" && rm -f "$zip" && zip -qr "$zip" . -x '*.zip' 'node_modules/*' '__pycache__/*' )
  echo "  deploying $(basename "$zip") ..."
  az webapp deploy -g "$RG" -n "$name" --src-path "$zip" --type zip --async false -o none

  url="https://${name}.azurewebsites.net"
  echo "  -> $url"
done

echo
echo "== provisioned apps =="
az webapp list -g "$RG" --query "[].{name:name, state:state, host:defaultHostName}" -o table
notify "DT Fleet — provisioning done" "4 demo app services provisioned in $RG"
echo
echo "RG=$RG  PREFIX=$PREFIX  SUFFIX=$SUFFIX"
echo "Smoke-test after ~1-2 min warmup, e.g.:  curl https://${PREFIX}-node-express-${SUFFIX}.azurewebsites.net/health"

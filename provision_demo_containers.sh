#!/usr/bin/env bash
# Provision demo CONTAINER Web Apps for exercising the sidecar path
# (dt_appservice_sidecar.py). Uses PUBLIC images (no local Docker build/push) and the
# existing demo subscription. Creates apps in BOTH modes so discovery + conversion get
# exercised:
#   dtfleet-ctr-dotnet  : .NET sample, sitecontainers mode, storage on  -> sidecar-ready
#   dtfleet-ctr-node    : Node sample, sitecontainers mode, storage on  -> sidecar-ready
#   dtfleet-ctr-py      : FastAPI,     sitecontainers mode               -> unsupported-runtime
#   dtfleet-ctr-java    : Tomcat,      legacy DOCKER| mode               -> needs-conversion
#
# Usage: ./provision_demo_containers.sh [plan|dotnet|node|py|java|all]   (default: all)
set -euo pipefail

RG="${RG:-dt-oneagent-fleet-demo}"
LOCATION="${LOCATION:-East US}"
PLAN="${PLAN:-dtfleet-ctr-plan}"
SKU="${SKU:-B2}"
API="2024-11-01"
WHAT="${1:-all}"

make_plan() {
  echo ">> plan $PLAN ($SKU, Linux) in $LOCATION"
  az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku "$SKU" -l "$LOCATION" -o none
}

# $1 name  $2 image  $3 targetPort  $4 storage(true/false)
make_sitecontainers_app() {
  local NAME=$1 IMAGE=$2 PORT=$3 STORAGE=$4
  echo ">> $NAME  (sitecontainers, $IMAGE :$PORT, storage=$STORAGE)"
  # create as a legacy container app first (simplest create), then convert to multi.
  az webapp create -g "$RG" -p "$PLAN" -n "$NAME" --container-image-name "$IMAGE" -o none
  local ID; ID=$(az webapp show -g "$RG" -n "$NAME" --query id -o tsv)
  # define the main site container
  az rest --method put --url "${ID}/sitecontainers/main?api-version=${API}" \
    --body "{\"properties\":{\"image\":\"${IMAGE}\",\"targetPort\":\"${PORT}\",\"isMain\":true,\"authType\":\"Anonymous\"}}" -o none
  # flip the app into multi-container (sitecontainers) mode
  az rest --method patch --url "${ID}/config/web?api-version=${API}" \
    --body '{"properties":{"linuxFxVersion":"sitecontainers"}}' -o none
  if [ "$STORAGE" = "true" ]; then
    az webapp config appsettings set -g "$RG" -n "$NAME" --settings WEBSITES_ENABLE_APP_SERVICE_STORAGE=true -o none
  fi
  az webapp restart -g "$RG" -n "$NAME" -o none
  echo "   created: https://${NAME}.azurewebsites.net"
}

# $1 name  $2 image  $3 port
make_legacy_app() {
  local NAME=$1 IMAGE=$2 PORT=$3
  echo ">> $NAME  (legacy DOCKER|, $IMAGE :$PORT)"
  az webapp create -g "$RG" -p "$PLAN" -n "$NAME" --container-image-name "$IMAGE" -o none
  az webapp config appsettings set -g "$RG" -n "$NAME" --settings WEBSITES_PORT="$PORT" -o none
  az webapp restart -g "$RG" -n "$NAME" -o none
  echo "   created: https://${NAME}.azurewebsites.net"
}

SUFFIX="${SUFFIX:-8ab598}"
[ "$WHAT" = "plan" ] || [ "$WHAT" = "all" ] && make_plan
case "$WHAT" in
  dotnet|all) make_sitecontainers_app "dtfleet-ctr-dotnet-${SUFFIX}" "mcr.microsoft.com/dotnet/samples:aspnetapp" 8080 true ;;
esac
case "$WHAT" in
  node|all) make_sitecontainers_app "dtfleet-ctr-node-${SUFFIX}" "mcr.microsoft.com/azuredocs/aci-helloworld:latest" 80 true ;;
esac
case "$WHAT" in
  py|all) make_sitecontainers_app "dtfleet-ctr-py-${SUFFIX}" "tiangolo/uvicorn-gunicorn-fastapi:python3.11" 80 true ;;
esac
case "$WHAT" in
  java|all) make_legacy_app "dtfleet-ctr-java-${SUFFIX}" "tomcat:9.0-jre11" 8080 ;;
esac
echo "done ($WHAT)."

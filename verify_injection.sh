#!/usr/bin/env bash
# verify_injection.sh — pull the newest container docker log for each demo app and
# run the gotchas triage checks: agent connected? active (not inactivated)? app
# launched? sensor armed?
set -uo pipefail
RG="${RG:-dt-oneagent-fleet-demo}"
PREFIX="${PREFIX:-dtfleet}"; SUFFIX="${SUFFIX:-8ab598}"
APPS=(node-express node-fastify py-flask py-fastapi)

for a in "${APPS[@]}"; do
  app="${PREFIX}-${a}-${SUFFIX}"
  echo "=================================================================="
  echo "### $app"
  CREDS=$(az webapp deployment list-publishing-credentials -g "$RG" -n "$app" -o json 2>/dev/null)
  U=$(echo "$CREDS" | jq -r '.publishingUserName'); P=$(echo "$CREDS" | jq -r '.publishingPassword')
  base="https://${app}.scm.azurewebsites.net/api/vfs/LogFiles"
  f=$(curl -s -u "$U:$P" "$base/" | jq -r '.[].name' 2>/dev/null | grep "default_docker.log" | sort | tail -1)
  curl -s -u "$U:$P" "$base/$f" -o "/tmp/verify_${a}.log" 2>/dev/null
  log="/tmp/verify_${a}.log"
  echo "  log: $f  ($(wc -l < "$log" 2>/dev/null) lines)"
  conn=$(grep -c "Connected to .*/communication" "$log" 2>/dev/null)
  inact=$(grep -icE "Agent inactivated|active state to OFF" "$log" 2>/dev/null)
  launch=$(grep -icE "listening on|Application startup complete|Booting worker|Started server|Uvicorn running|Listening at" "$log" 2>/dev/null)
  tracing=$(grep -c "allowedFeatures.*Tracing" "$log" 2>/dev/null)
  svc=$(grep -oE "OTEL servicename [^ ]*\.+ .*" "$log" 2>/dev/null | tail -1)
  preload=$(grep -icE "cannot be preloaded" "$log" 2>/dev/null)
  printf "  connected=%s  inactivated=%s  tracing-allowed=%s  app-launched=%s  preload-errors=%s\n" \
    "$conn" "$inact" "$tracing" "$launch" "$preload"
  [ -n "$svc" ] && echo "  $svc"
  echo "  --- sensor/runtime evidence ---"
  grep -iE "Reporting package list|Python version|AppName|detected_name|PaasVendorData - Azure detected" "$log" 2>/dev/null | head -3 | sed 's/^/  /' | cut -c1-160
done

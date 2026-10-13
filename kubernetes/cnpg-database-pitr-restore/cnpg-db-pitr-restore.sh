#!/usr/bin/env bash
# Per-database point-in-time restore for a shared CloudNativePG cluster.
#
#   1. create a small staging Cluster recovered from the source cluster's
#      barman object store to TARGET_TIME (read-only use of the source backups)
#   2. pg_dump -Fc ONE database from staging into a PVC (and optionally S3)
#   3. mode=restore: pg_restore that one database into the source cluster
#      mode=dump-only: stop, leave the dump on the PVC
#   4. delete the staging Cluster (unless KEEP_STAGING=true)
#
# Needs: kubectl, jq, cluster-admin-ish rights in NS (create Cluster/Job/Pod/PVC,
# pods/exec on the source primary). Every setting is an env var, see README.
set -Eeuo pipefail

: "${NS:=databases}"                    # namespace of the source cluster (staging lives here too)
: "${SRC_CLUSTER:?set SRC_CLUSTER}"     # source (production) cluster name
: "${DB:?set DB}"                       # database to recover
: "${TARGET_TIME:?set TARGET_TIME, e.g. '2026-01-31 09:15:00+00'}"
: "${MODE:=dump-only}"                  # dump-only | restore
: "${RESTORE_DB:=$DB}"                  # restore mode: target database name on the source cluster
: "${STAGING_SIZE:=2Gi}"                # must exceed the size of the recovered cluster
: "${STORAGE_CLASS:=}"                  # default: the source cluster's class
: "${PG_IMAGE:=}"                       # default: the source cluster's imageName
: "${DUMP_DEST:=pvc}"                   # pvc | s3 (s3 = pvc plus an upload)
: "${DUMP_PVC:=}"                       # existing PVC to write into; default: create one
: "${DUMP_PVC_SIZE:=1Gi}"
: "${S3_URI:=}"                         # s3://bucket/prefix/ when DUMP_DEST=s3
: "${S3_SECRET:=}"                      # Secret with ACCESS_KEY_ID / ACCESS_SECRET_KEY
: "${S3_ENDPOINT:=}"
: "${AWS_CLI_IMAGE:=amazon/aws-cli:2.17.0}"
: "${SAFETY_DUMP:=true}"                # restore mode: dump the live target first
: "${KEEP_STAGING:=false}"
: "${TIMEOUT:=20m}"

case "$MODE" in dump-only|restore) ;; *) echo "MODE must be dump-only or restore" >&2; exit 2;; esac
case "$DUMP_DEST" in pvc|s3) ;; *) echo "DUMP_DEST must be pvc or s3" >&2; exit 2;; esac
case "$RESTORE_DB" in postgres|template0|template1) echo "refusing RESTORE_DB=$RESTORE_DB" >&2; exit 2;; esac
[[ $DUMP_DEST == s3 && ( -z $S3_URI || -z $S3_SECRET ) ]] && { echo "S3_URI and S3_SECRET required for s3" >&2; exit 2; }
[[ $DB =~ ^[A-Za-z0-9_]+$ && $RESTORE_DB =~ ^[A-Za-z0-9_]+$ ]] || { echo "DB names: [A-Za-z0-9_] only" >&2; exit 2; }

k() { kubectl -n "$NS" "$@"; }
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

RUN=$(date -u +%m%d%H%M%S)
STG="${SRC_CLUSTER}-pitr-${RUN}"
[[ $STG == "$SRC_CLUSTER" ]] && exit 3
[[ -n $DUMP_PVC ]] || { DUMP_PVC="${STG}-dump"; NEW_PVC=1; }
DUMP_FILE="${DB}_$(tr -cd '0-9' <<<"$TARGET_TIME" | cut -c1-14).dump"
JOB="${STG}-dump"; READER="${STG}-reader"

SRC_JSON=$(k get clusters.postgresql.cnpg.io "$SRC_CLUSTER" -o json)
STORE=$(jq -e '.spec.backup.barmanObjectStore' <<<"$SRC_JSON")   # fails if prod has no object store backup
[[ -n $PG_IMAGE ]] || PG_IMAGE=$(jq -r '.spec.imageName' <<<"$SRC_JSON")
[[ -n $STORAGE_CLASS ]] || STORAGE_CLASS=$(jq -r '.spec.storage.storageClass // ""' <<<"$SRC_JSON")
PRIMARY=$(jq -r '.status.currentPrimary' <<<"$SRC_JSON")

cleanup() {
  rc=$?
  k delete pod "$READER" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  k delete job "$JOB" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [[ $KEEP_STAGING == true ]]; then log "KEEP_STAGING=true: staging cluster $STG left running"
  else k delete clusters.postgresql.cnpg.io "$STG" --ignore-not-found --wait=false >/dev/null 2>&1 && log "staging $STG deleted"; fi
  [[ -n ${NEW_PVC:-} ]] && log "dump PVC kept: $DUMP_PVC (file $DUMP_FILE)"
  exit $rc
}
trap cleanup EXIT

log "1/4 staging cluster $STG recovering $SRC_CLUSTER to $TARGET_TIME"
# serverName must be the folder the source archives under (its cluster name unless overridden).
# No spec.backup on staging: it never archives, so it cannot write to the source path.
jq -n --arg n "$STG" --arg ns "$NS" --arg src "$SRC_CLUSTER" --arg t "$TARGET_TIME" \
  --arg img "$PG_IMAGE" --arg sz "$STAGING_SIZE" --arg sc "$STORAGE_CLASS" --argjson store "$STORE" '
{apiVersion:"postgresql.cnpg.io/v1",kind:"Cluster",
 metadata:{name:$n,namespace:$ns,labels:{"app.kubernetes.io/part-of":"cnpg-pitr-restore","cnpg-pitr/source":$src}},
 spec:{instances:1,imageName:$img,enableSuperuserAccess:true,
  storage:({size:$sz}+(if $sc=="" then {} else {storageClass:$sc} end)),
  resources:{requests:{cpu:"100m",memory:"256Mi"},limits:{cpu:"1",memory:"1Gi"}},
  bootstrap:{recovery:{source:"origin",recoveryTarget:{targetTime:$t}}},
  externalClusters:[{name:"origin",barmanObjectStore:($store+{serverName:($store.serverName // $src)})}]}}' \
  | k apply -f - >/dev/null
k wait --for=condition=Ready "clusters.postgresql.cnpg.io/$STG" --timeout="$TIMEOUT" >/dev/null
log "staging ready"

STG_POD=$(k get cluster.postgresql.cnpg.io "$STG" -o jsonpath='{.status.currentPrimary}')
OWNER=$(k exec "$STG_POD" -c postgres -- psql -Atc "select pg_get_userbyid(datdba) from pg_database where datname='$DB'")
[[ -n $OWNER ]] || { echo "database $DB does not exist in staging at $TARGET_TIME" >&2; exit 4; }
log "database $DB present at target time, owner $OWNER"

log "2/4 pg_dump -Fc $DB -> pvc/$DUMP_PVC/$DUMP_FILE"
if [[ -n ${NEW_PVC:-} ]]; then
  jq -n --arg n "$DUMP_PVC" --arg sz "$DUMP_PVC_SIZE" --arg sc "$STORAGE_CLASS" '
  {apiVersion:"v1",kind:"PersistentVolumeClaim",metadata:{name:$n},
   spec:({accessModes:["ReadWriteOnce"],resources:{requests:{storage:$sz}}}+(if $sc=="" then {} else {storageClassName:$sc} end))}' | k apply -f - >/dev/null
fi
jq -n --arg n "$JOB" --arg img "$PG_IMAGE" --arg stg "$STG" --arg pvc "$DUMP_PVC" --arg db "$DB" \
  --arg f "$DUMP_FILE" --arg s3 "$S3_URI" --arg s3s "$S3_SECRET" --arg ep "$S3_ENDPOINT" \
  --arg aws "$AWS_CLI_IMAGE" --arg dest "$DUMP_DEST" '
def sec(n;k): {valueFrom:{secretKeyRef:{name:n,key:k}}};
{apiVersion:"batch/v1",kind:"Job",metadata:{name:$n},
 spec:{backoffLimit:0,ttlSecondsAfterFinished:3600,template:{spec:{
  restartPolicy:"Never",
  securityContext:{runAsNonRoot:true,runAsUser:26,fsGroup:26,seccompProfile:{type:"RuntimeDefault"}},
  volumes:[{name:"dump",persistentVolumeClaim:{claimName:$pvc}},
           {name:"ca",secret:{secretName:($stg+"-ca")}}],
  initContainers:[{name:"pg-dump",image:$img,
   securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}},
   env:[{name:"PGHOST",value:($stg+"-rw")},{name:"PGDATABASE",value:$db},
        {name:"PGUSER"}+sec(($stg+"-superuser");"username"),
        {name:"PGPASSWORD"}+sec(($stg+"-superuser");"password"),
        {name:"PGSSLMODE",value:"verify-full"},{name:"PGSSLROOTCERT",value:"/ca/ca.crt"}],
   volumeMounts:[{name:"dump",mountPath:"/dump"},{name:"ca",mountPath:"/ca"}],
   command:["sh","-ec",("pg_dump -Fc -f /dump/"+$f+" && pg_restore -l /dump/"+$f+" >/dev/null && ls -l /dump/"+$f)]}],
  containers:[if $dest=="s3" then
   {name:"upload",image:$aws,
    securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}},
    env:([{name:"AWS_ACCESS_KEY_ID"}+sec($s3s;"ACCESS_KEY_ID"),{name:"AWS_SECRET_ACCESS_KEY"}+sec($s3s;"ACCESS_SECRET_KEY"),
          {name:"AWS_DEFAULT_REGION",value:"us-east-1"}]),
    volumeMounts:[{name:"dump",mountPath:"/dump"}],
    command:["sh","-ec",("aws s3 cp "+(if $ep=="" then "" else "--endpoint-url "+$ep+" " end)+"/dump/"+$f+" "+$s3+(if ($s3|endswith("/")) then "" else "/" end)+$f)]}
   else {name:"done",image:$img,command:["true"],
    securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}}} end]}}}}' \
  | k apply -f - >/dev/null
k wait --for=condition=Complete "job/$JOB" --timeout="$TIMEOUT" >/dev/null || { k logs "job/$JOB" --all-containers --tail=50 >&2 || true; exit 5; }
k logs "job/$JOB" -c pg-dump | tail -2

if [[ $MODE == dump-only ]]; then
  log "3/4 dump-only: stopping. Dump: pvc/$DUMP_PVC/$DUMP_FILE (owner role of $DB: $OWNER)"
else
  log "3/4 restore $DB@$TARGET_TIME -> $SRC_CLUSTER/$RESTORE_DB via $PRIMARY"
  jq -n --arg n "$READER" --arg img "$PG_IMAGE" --arg pvc "$DUMP_PVC" '
  {apiVersion:"v1",kind:"Pod",metadata:{name:$n},spec:{restartPolicy:"Never",
   securityContext:{runAsNonRoot:true,runAsUser:26,fsGroup:26,seccompProfile:{type:"RuntimeDefault"}},
   volumes:[{name:"dump",persistentVolumeClaim:{claimName:$pvc}}],
   containers:[{name:"r",image:$img,command:["sleep","3600"],
    securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}},
    volumeMounts:[{name:"dump",mountPath:"/dump"}]}]}}' | k apply -f - >/dev/null
  k wait --for=condition=Ready "pod/$READER" --timeout=5m >/dev/null
  P() { k exec "$PRIMARY" -c postgres -- "$@"; }          # local socket, peer auth: no password handled
  PI() { k exec -i "$PRIMARY" -c postgres -- "$@"; }
  P psql -Atc "select 1 from pg_roles where rolname='$OWNER'" | grep -q 1 \
    || { echo "role $OWNER does not exist on $SRC_CLUSTER; create it first" >&2; exit 6; }
  EXISTS=$(P psql -Atc "select 1 from pg_database where datname='$RESTORE_DB'")
  if [[ -z $EXISTS ]]; then
    P psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$RESTORE_DB\" OWNER \"$OWNER\""
  elif [[ $SAFETY_DUMP == true ]]; then
    SAFE="pre-restore_${RESTORE_DB}_${RUN}.dump"
    P pg_dump -Fc "$RESTORE_DB" | k exec -i "$READER" -- sh -c "cat > /dump/$SAFE"
    log "safety dump of live $RESTORE_DB: pvc/$DUMP_PVC/$SAFE"
  fi
  # Scope: -d names the one target database; no --create, no cluster-wide globals.
  # --single-transaction: a failed restore leaves the database as it was.
  k exec "$READER" -- cat "/dump/$DUMP_FILE" \
    | PI pg_restore --clean --if-exists --no-owner --role="$OWNER" --single-transaction -d "$RESTORE_DB"
  log "restored. tables in $RESTORE_DB: $(P psql -Atd "$RESTORE_DB" -c "select count(*) from pg_tables where schemaname not in ('pg_catalog','information_schema')")"
fi
log "4/4 teardown"

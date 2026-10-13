# CNPG per-database point-in-time restore

Restore ONE database of a shared CloudNativePG cluster to a past moment, without
touching the other databases on it. `cnpg-db-pitr-restore.sh` does the whole run
with `kubectl` and `jq`; no operator changes, no manifests to edit.

## What it does

1. Creates a small staging `Cluster` (1 instance) with `bootstrap.recovery` and
   `recoveryTarget.targetTime`, reading the source cluster's `barmanObjectStore`
   (copied from its `spec.backup`). Staging has no `spec.backup`, so it never
   archives and cannot write to the source's WAL path.
2. Runs a Job that `pg_dump -Fc` the one database from staging into a PVC
   (optionally also uploads to S3).
3. `MODE=dump-only` stops here. `MODE=restore` runs `pg_restore` of that dump into
   the source cluster:
   `--clean --if-exists --no-owner --role=<db owner> --single-transaction -d <RESTORE_DB>`.
   No `--create`, only the named database is addressed, and a failed restore rolls back.
4. Deletes the staging cluster, Job and helper pod (`KEEP_STAGING=true` to keep).
   The dump PVC is always kept.

Restore runs `pg_restore` through `kubectl exec` on the source primary (local
socket, peer auth), so no superuser password is needed or stored. The dump is
streamed from the PVC through your terminal's `kubectl`. It creates `RESTORE_DB`
(owned by the original owner role, which must exist) if it is missing. When the
target exists and `SAFETY_DUMP=true`, it first dumps the live database to the same
PVC as `pre-restore_<db>_<run>.dump`.

## Run

```bash
export KUBECONFIG=~/.kube/prod.yaml

# dump only: leaves /dump/<db>_<time>.dump on a new PVC
SRC_CLUSTER=pg-main DB=appdb TARGET_TIME='2026-01-31 09:15:00+00' \
  MODE=dump-only ./cnpg-db-pitr-restore.sh

# restore into a new database to compare and swap later
SRC_CLUSTER=pg-main DB=appdb TARGET_TIME='2026-01-31 09:15:00+00' \
  MODE=restore RESTORE_DB=appdb_restored ./cnpg-db-pitr-restore.sh

# restore over the original (same name, --clean --if-exists)
SRC_CLUSTER=pg-main DB=appdb TARGET_TIME='2026-01-31 09:15:00+00' \
  MODE=restore ./cnpg-db-pitr-restore.sh
```

| Variable | Default | Meaning |
|---|---|---|
| `SRC_CLUSTER` | required | source cluster name |
| `NS` | `databases` | source namespace (staging is created here too) |
| `DB` | required | database to recover |
| `TARGET_TIME` | required | PITR time, e.g. `2026-01-31 09:15:00+00` (after the first base backup, covered by archived WAL) |
| `MODE` | `dump-only` | `dump-only` or `restore` |
| `RESTORE_DB` | `$DB` | target database on the source cluster |
| `STAGING_SIZE` | `2Gi` | must exceed the recovered cluster's size |
| `STORAGE_CLASS` | source's | storage class for staging and the dump PVC |
| `PG_IMAGE` | source's `imageName` | staging and Job image (must be the same major as the source) |
| `DUMP_DEST` | `pvc` | `pvc`, or `s3` (PVC plus upload; needs `S3_URI`, `S3_SECRET`, optional `S3_ENDPOINT`, `AWS_CLI_IMAGE`) |
| `DUMP_PVC` | new `<staging>-dump` | existing PVC to write into |
| `DUMP_PVC_SIZE` | `1Gi` | size of the created PVC |
| `SAFETY_DUMP` | `true` | dump the live target before restoring over it |
| `KEEP_STAGING` | `false` | keep the staging cluster for inspection |
| `TIMEOUT` | `20m` | wait for staging and the dump Job |

Requirements: the source cluster has `spec.backup.barmanObjectStore`, working WAL
archiving, and at least one completed base backup before `TARGET_TIME`. The
credentials Secret it references must be in `NS`. Exec rights on the source primary
are needed for `restore` mode.

## Verify

```bash
# before: note a time, and force the WAL segment out so the change is archived now
kubectl -n $NS exec <primary> -c postgres -- psql -Atc "select now(), pg_switch_wal()"
# after a run: compare rows, owners, and confirm other databases are untouched
kubectl -n $NS exec <primary> -c postgres -- psql -d appdb_restored -c "select count(*) from some_table"
kubectl -n $NS get cluster,pods,pvc,job | grep pitr       # nothing left but the dump PVC
```

Read the dump of a dump-only run manually:

```bash
kubectl -n $NS run dumpview --image=<PG_IMAGE> --overrides='{"spec":{"securityContext":{"fsGroup":26},"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"<dump pvc>"}}],"containers":[{"name":"dumpview","image":"<PG_IMAGE>","command":["sleep","3600"],"volumeMounts":[{"name":"d","mountPath":"/dump"}]}]}}'
kubectl -n $NS exec dumpview -- pg_restore -l /dump/<file>.dump
kubectl -n $NS exec dumpview -- cat /dump/<file>.dump | kubectl -n $NS exec -i <primary> -c postgres -- \
  pg_restore --clean --if-exists --no-owner --role=<owner> --single-transaction -d <target_db>
kubectl -n $NS delete pod dumpview; kubectl -n $NS delete pvc <dump pvc>
```

## Tested

Source: 2-instance PostgreSQL 17 cluster, daily base backup, `archive_timeout=5min`,
CloudNativePG with in-tree `barmanObjectStore`. A throwaway database with a table of 5
rows was created, the time noted, then 2 rows deleted, 1 changed, 1 inserted, with
`pg_switch_wal()` after each step.

| Run | Result | Time |
|---|---|---|
| `dump-only`, target between the two states | dump holds the 5 original rows; staging removed, PVC kept | 76 s end to end |
| `restore`, `RESTORE_DB=<new name>` | new database created, 5 original rows, table owner preserved, original DB still held the changed rows | about 70 s |
| `restore`, same name, existing PVC | safety dump written, `--clean --if-exists` replaced the table, 5 original rows back | about 75 s |

Other databases on the cluster were listed before and after: unchanged. Test database,
role, staging clusters and PVCs were deleted afterwards.

## Not covered

- `DUMP_DEST=s3` is written but was not run.
- Extensions, roles and grants outside the one database are not restored; the
  owner role must already exist on the source cluster.
- Source clusters using the Barman Cloud plugin (`ObjectStore`) instead of the in-tree
  `barmanObjectStore` need the `externalClusters` block changed to `plugin:` form.
  CloudNativePG warns the in-tree form is removed in 1.31.
- Active connections holding locks on the target make `--clean` wait; stop the
  application first for an in-place restore.

#!/usr/bin/env bash
# Prove the full lifecycle from scratch: cross, dedupe, prune, and RE-cross.
#
# The last one is the point. Pruning the far-side store is only safe if the low
# side forgets the blob at the same moment, so that a later need for it is met by
# sending it again. If that is wrong the failure is silent and permanent, so this
# test drives a blob out of the desired set and then pulls it back in.
#
# The semver window does the work. With KEEP=2:
#
#   T1  seed 1.0.0, 1.1.0        desired {1.1.0, 1.0.0}   everything crosses
#   T2  seed 2.0.0               desired {2.0.0, 1.1.0}   1.0.0 falls out -> pruned
#   T3  delete 2.0.0 upstream    desired {1.1.0, 1.0.0}   1.0.0 returns -> must re-cross
#
#   usage: scripts/test-lifecycle.sh        (destroys and rebuilds the whole lab)
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd); cd "$here"
LOW=${LOW_REGISTRY:-localhost:18081}; HIGH=${HIGH_REGISTRY:-localhost:18082}
U=${QUAY_USER:-admin}; P=${QUAY_PASS:-quayadmin123}; ORG=${QUAY_ORG:-demo}
DC="sudo docker compose"
pass=0; fail=0

ok(){ printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
chk(){ if [ "$2" = "$3" ]; then ok "$1 ($2)"; else no "$1 — expected [$3], got [$2]"; fi; }
step(){ printf '\n\033[1m== %s\033[0m\n' "$1"; }

api(){ curl -fsS -H "Authorization: Bearer $(cat .secrets/${1}.token)" "http://${2}/api/v1${3}"; }
redis(){ $DC exec -T dedupe-redis redis-cli "$@"; }

wait_transfer(){ # wait for the receive flow to land a transfer, then import
  local i
  for i in $(seq 1 60); do ls data/high-store/incoming/ 2>/dev/null | grep -q . && break; sleep 5; done
  ls data/high-store/incoming/ 2>/dev/null | grep -q . || { echo "transfer never arrived"; exit 1; }
  local inc; inc=$(ls -dt data/high-store/incoming/transfer-* | head -1)
  CROSSED=$(tar -tf "data/diode/received/$(basename "$inc").tar" | grep -c 'blobs/sha256/' || true)
  scripts/import.sh >/tmp/import.log 2>&1 || { echo "import failed:"; tail -20 /tmp/import.log; exit 1; }
}

# semver tags only: dev/latest float with the newest version and are asserted apart
high_tags(){ api high "$HIGH" "/repository/$ORG/$1/tag/?limit=100&onlyActiveTags=true" \
  | python3 -c 'import json,re,sys
sv=re.compile(r"^\d+\.\d+\.\d+$")
print(" ".join(sorted({t["name"] for t in json.load(sys.stdin)["tags"] if sv.match(t["name"])})))'; }

# the layer that only demo/app:$1 has, read out of the high-side store
unique_layer(){ python3 - "$1" <<'PY'
import json,sys,os
ver=sys.argv[1]; store="data/high-store/oci"
idx=json.load(open(f"{store}/index.json"))
ref={m["annotations"]["org.opencontainers.image.ref.name"]:m["digest"] for m in idx["manifests"]}
key=f"demo/app:{ver}"
if key not in ref: print(""); raise SystemExit
def layers(d):
    p=f"{store}/blobs/sha256/{d.split(':')[1]}"
    if not os.path.isfile(p): return []
    return [l["digest"].split(":")[1] for l in json.load(open(p)).get("layers",[])]
mine=set(layers(ref[key]))
others=set()
for r,d in ref.items():
    # a floating tag can point at the SAME digest, which is the same image, not another one
    if d != ref[key]: others |= set(layers(d))
u=sorted(mine-others)
print(u[0] if u else "")
PY
}

step "0. tear everything down and rebuild from nothing"
$DC down -v >/dev/null 2>&1 || true
# NiFi writes into these as uid 1000, so a plain rm cannot clear them. Teardown is
# destructive by definition; this is the one place elevation is warranted.
sudo rm -rf data/low-export data/diode data/high-store
rm -f .secrets/low.token .secrets/high.token
mkdir -p data/low-export data/diode data/high-store && chmod 777 data data/*
# NiFi runs as uid 1000 and creates the per-transfer directory itself. Renaming a
# directory needs write permission ON that directory, so import.sh's move into
# imported/ fails unless new subdirectories inherit rights for this user. A default
# ACL is the least invasive way to grant that; Linux only, and a no-op elsewhere.
if command -v setfacl >/dev/null 2>&1; then
  sudo setfacl -R -m "u:$(id -un):rwx" -d -m "u:$(id -un):rwx" data/low-export data/diode data/high-store
fi
scripts/quay-config.sh low  "$LOW"  >/dev/null
scripts/quay-config.sh high "$HIGH" >/dev/null
$DC up -d >/dev/null 2>&1
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://$LOW/v2/")" = 401 ] &&
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://$HIGH/v2/")" = 401 ] && break
  sleep 5
done
scripts/quay-init.sh low  "http://$LOW"  >/dev/null
scripts/quay-init.sh high "http://$HIGH" >/dev/null
python3 scripts/nifi-flow.py --reset >/dev/null
echo "  rebuilt: both registries initialised, both flows running"
chk "ledger starts empty" "$(redis dbsize | tr -d '\r')" "0"

step "1. first transfer — 1.0.0 and 1.1.0 both cross"
scripts/seed-images.sh 1.0.0 1.1.0 >/dev/null 2>&1
scripts/export.sh 2 >/tmp/e1.log 2>&1; tail -1 /tmp/e1.log | sed 's/^/  /'
wait_transfer
[ "$CROSSED" -gt 0 ] && ok "blobs crossed on a cold ledger ($CROSSED)" || no "nothing crossed on the first transfer"
chk "high side holds both versions" "$(high_tags app)" "1.0.0 1.1.0"
DOOMED=$(unique_layer 1.0.0)
if [ -z "$DOOMED" ]; then
  no "could not isolate a layer unique to 1.0.0 — every later check would be vacuous, stopping"
  echo "  store refs:"; python3 -c '
import json
i=json.load(open("data/high-store/oci/index.json"))
for m in i["manifests"]: print("   ", m["annotations"].get("org.opencontainers.image.ref.name"), m["digest"][:20])'
  exit 1
fi
ok "identified the layer unique to 1.0.0 (${DOOMED:0:12})"
chk "that layer is in the ledger" "$(redis exists "$DOOMED" | tr -d '\r')" "1"
[ -f "data/high-store/oci/blobs/sha256/$DOOMED" ] && ok "that layer is in the far store" || no "layer missing from the far store"

step "2. 2.0.0 arrives — 1.0.0 falls out of the window and is pruned"
scripts/seed-images.sh 2.0.0 >/dev/null 2>&1
scripts/export.sh 2 >/tmp/e2.log 2>&1; grep -E 'ledger:|wrote' /tmp/e2.log | sed 's/^/  /'
wait_transfer
grep -q 'pruned' /tmp/import.log && grep 'pruned' /tmp/import.log | tail -1 | sed 's/^/  /' || true
chk "high side moved to the new window" "$(high_tags app)" "1.1.0 2.0.0"
chk "the ledger has forgotten the doomed layer" "$(redis exists "$DOOMED" | tr -d '\r')" "0"
if [ ! -f "data/high-store/oci/blobs/sha256/$DOOMED" ]; then ok "the far store no longer holds it"; else no "far store still holds a pruned layer"; fi

step "3. the catch-22 test — 2.0.0 is withdrawn, so 1.0.0 must come back"
curl -fsS -X DELETE -H "Authorization: Bearer $(cat .secrets/low.token)" \
  "http://$LOW/api/v1/repository/$ORG/app/tag/2.0.0" >/dev/null
curl -fsS -X DELETE -H "Authorization: Bearer $(cat .secrets/low.token)" \
  "http://$LOW/api/v1/repository/$ORG/api/tag/2.0.0" >/dev/null
echo "  withdrew 2.0.0 upstream; the newest two are now 1.1.0 and 1.0.0"
scripts/export.sh 2 >/tmp/e3.log 2>&1; tail -1 /tmp/e3.log | sed 's/^/  /'
wait_transfer
[ "$CROSSED" -gt 0 ] && ok "the forgotten layer was sent again ($CROSSED blob(s) crossed)" \
                     || no "NOTHING crossed — the ledger still believed it had sent a blob it had forgotten"
[ -f "data/high-store/oci/blobs/sha256/$DOOMED" ] && ok "the far store holds it once more" || no "far store never got it back"
chk "high side is back to the earlier window" "$(high_tags app)" "1.0.0 1.1.0"

step "4. the restored image is genuinely usable, not just present"
rm -rf /tmp/lifecycle-probe
if skopeo copy -q "oci:data/high-store/oci:demo/app:1.0.0" dir:/tmp/lifecycle-probe 2>/tmp/sk.err; then
  ok "the far store can rebuild demo/app:1.0.0 standalone"
else
  no "far store cannot produce the image: $(tail -1 /tmp/sk.err)"
fi
LO=$(skopeo inspect --tls-verify=false --creds "$U:$P" "docker://$LOW/$ORG/app:1.0.0"  | python3 -c 'import json,sys;print(json.load(sys.stdin)["Digest"])')
HI=$(skopeo inspect --tls-verify=false --creds "$U:$P" "docker://$HIGH/$ORG/app:1.0.0" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Digest"])')
chk "digest on the high side matches the low side" "$HI" "$LO"
rm -rf /tmp/lifecycle-probe

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$pass" "$fail"
[ "$fail" -eq 0 ]

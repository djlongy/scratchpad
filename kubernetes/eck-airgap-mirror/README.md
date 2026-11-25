# ECK air-gap mirror

## TL;DR

Mirror the full Elastic integration catalogue, Defend artifacts, GeoIP, ML models and the AI Assistant documentation into an air-gapped Kubernetes cluster, then point an existing ECK stack at it: `scripts/mirror-bundle.sh -v 9.5.2 -o ./bundle-9.5.2 --dry-run`.

## Requirements

- Connected side: `bash`, `curl`, `jq`, `skopeo` 1.13 or later, `python3` 3.9 or later, `unzip`, and about 30 GiB free disk.
- Air-gapped side: Kubernetes with an enforcing `NetworkPolicy` CNI, cert-manager, an internal image registry, an existing ECK stack (Elastic Stack 9.x), `kubectl`, `skopeo`, `curl` 7.75 or later.
- One node with at least 80 GiB of image disk for the registry pod (25 GiB compressed image plus 45 GiB extracted).
- An ML node in the Elasticsearch cluster if you want the AI Assistant documentation (it needs ELSER).

## What the stack fetches and where it comes from here

| Content | Upstream | Served here by | Consumer setting |
|---|---|---|---|
| Integrations, detection rules, prebuilt ML jobs, dashboards | `epr.elastic.co` | `epr` pod | Kibana `xpack.fleet.registryUrl` |
| Agent, Fleet Server, Endpoint binaries and the upgrade key | `artifacts.elastic.co` | bucket `elastic-mirror`, `downloads/` | Fleet agent binary source |
| Defend malware and behaviour artifacts | `artifacts.security.elastic.co` | bucket `elastic-mirror`, `downloads/endpoint/` | Defend `<os>.advanced.artifacts.global.base_url` |
| GeoIP databases | `geoip.elastic.co` | bucket `elastic-mirror`, `geoip/` | Elasticsearch `ingest.geoip.downloader.endpoint` |
| ELSER, e5 and rerank models | `ml-models.elastic.co` | bucket `elastic-mirror`, `models/` | Elasticsearch `xpack.ml.model_repository` |
| AI Assistant product documentation and Security Labs | `kibana-knowledge-base-artifacts.elastic.co` | bucket `elastic-kb` | Kibana `xpack.productDocBase.artifactRepositoryUrl` |

The registry image decides which rules and integrations you get. `distribution:production` is the full catalogue with the newest packages. `distribution:<version>` is the full catalogue as released with that stack version, so its rules are older. `lite-<version>` holds about 17 packages and is not enough for a full install.

## 1. Export on the connected side

1. Set the version. It must equal the Kibana version on the air-gapped side.

   ```sh
   export V=9.5.2
   ```

2. Read the sizes. This downloads nothing.

   ```sh
   scripts/mirror-bundle.sh -v "$V" -o ./bundle-$V --dry-run
   ```

   Measured for 9.5.2, linux x86_64: registry image about 24.5 GiB, the stack images about 2 GiB, files about 2.9 GiB (agents 464 MiB, Defend 45 MiB, GeoIP 30 MiB, models 2.2 GiB, documentation 80 MiB).

3. Export. Re-running resumes where it stopped.

   ```sh
   export EPR_VARIANT=production
   export AGENT_ARCHS="linux-x86_64 windows-x86_64"   # the platforms your agents run on
   export AGENT_PACKAGES="tar.gz msi"                 # add deb or rpm as needed
   export EXTRA_IMAGES="docker.io/pgsty/minio:RELEASE.2026-08-04T00-00-00Z docker.io/curlimages/curl:8.11.1"
   scripts/mirror-bundle.sh -v "$V" -o ./bundle-$V
   ```

   - `EXTRA_IMAGES` carries the store and the bootstrap images. Use any MinIO community build that ships `mc`.
   - The documentation export takes `kb-product-doc-{security,kibana,observability,elasticsearch,openapi}-<major.minor>.zip` and the newest `security-labs-*.zip`. These are the ELSER builds. For the multilingual e5 builds set `KB_SUFFIX=--.multilingual-e5-small-elasticsearch`.
   - To export only some parts later: `COMPONENTS="endpoint geoip kb"` (parts: `images agent endpoint geoip models kb`).

4. Check the bundle and record its fingerprint on the transfer paperwork.

   ```sh
   (cd bundle-$V && sha256sum -c --quiet MANIFEST.sha256)   # macOS: shasum -a 256 -c --quiet
   sha256sum bundle-$V/MANIFEST.sha256
   ```

   `bundle-$V/bundle.env` records the export time and the `security_detection_engine` version the public registry served.

## 2. Transfer

1. Copy `bundle-$V/` and this folder's `scripts/` and `k8s/` to the transfer medium. Split files over a size limit with `split -b 4000m <file> <file>.part-` and rejoin with `cat <file>.part-* > <file>`.
2. Expect the scanner to flag `artifacts/downloads/endpoint/`: those are Elastic's detection artifacts. Allow-list that directory per site policy.
3. On the air-gapped side, compare the fingerprint of `MANIFEST.sha256` with the paperwork before anything else.

## 3. Deploy the mirror on the air-gapped side

1. Push the images. Note the digest printed for each one: `docker-archive` rebuilds the manifest, so the digest differs from upstream.

   ```sh
   skopeo login registry.example.internal
   scripts/load-bundle.sh -b ./bundle-$V -r registry.example.internal/elastic
   ```

2. Put those digests in `k8s/epr.yaml`, `k8s/minio.yaml`, `k8s/bootstrap-job.yaml` and `k8s/egress-probe.yaml`. Set the storage class in `k8s/minio.yaml`, the ECK namespace in `k8s/networkpolicy.yaml`, and the host and cluster issuer in `k8s/ingress.yaml`.
3. Label the node that holds the registry pod.

   ```sh
   kubectl label node <node> elastic-mirror/epr=true
   ```

4. Create the namespace and secrets. Keep the generated values in your secret store.

   ```sh
   kubectl apply -f k8s/namespace.yaml
   kubectl -n elastic-mirror create secret generic minio-root \
     --from-literal=MINIO_ROOT_USER=mirror-admin \
     --from-literal=MINIO_ROOT_PASSWORD="$(openssl rand -base64 24)"
   kubectl -n elastic-mirror create secret generic minio-writer \
     --from-literal=WRITER_USER=transfer-writer \
     --from-literal=WRITER_PASSWORD="$(openssl rand -base64 24)"
   kubectl -n elastic-mirror create secret docker-registry registry-pull \
     --docker-server=registry.example.internal --docker-username=<puller> --docker-password="$PULL_TOKEN"
   ```

5. Apply and wait. The full registry indexes for up to 30 minutes before it listens. The startup probe allows that.

   ```sh
   kubectl apply -k k8s
   kubectl -n elastic-mirror wait --for=condition=complete job/minio-bootstrap job/minio-writer-bootstrap --timeout=10m
   kubectl -n elastic-mirror rollout status deploy/epr --timeout=60m
   ```

   Under Argo CD the two Jobs run after every sync. With plain `kubectl`, delete a Job before you apply it again.

6. Upload the files with the write-only user. Every object goes up as one PUT, so its `ETag` is the plain MD5 that Elastic Endpoint needs.

   ```sh
   kubectl -n elastic-mirror get secret elastic-mirror-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > mirror-ca.crt
   kubectl -n elastic-mirror port-forward svc/minio 9000:9000 &
   export S3_USER=transfer-writer
   S3_PASSWORD="$(kubectl -n elastic-mirror get secret minio-writer -o jsonpath='{.data.WRITER_PASSWORD}' | base64 -d)"
   export S3_PASSWORD
   scripts/load-bundle.sh -b ./bundle-$V -s https://minio.elastic-mirror.svc:9000 -c mirror-ca.crt
   ```

   The certificate names the service, not `127.0.0.1`. Map `minio.elastic-mirror.svc` to `127.0.0.1` in `/etc/hosts` on the upload host while the port-forward runs.

7. Prove the pods cannot reach the internet. Expect `exit=28` twice and `200` once.

   ```sh
   kubectl -n elastic-mirror apply -f k8s/egress-probe.yaml
   kubectl -n elastic-mirror wait --for=condition=complete job/egress-probe --timeout=120s
   kubectl -n elastic-mirror logs job/egress-probe && kubectl -n elastic-mirror delete job/egress-probe
   ```

## 4. Check the mirror before you touch the stack

Run from a host that resolves the service names (or from a pod), with the CA file:

```sh
scripts/verify-mirror.sh -v "$V" \
  -e https://epr.elastic-mirror.svc:8080 \
  -f https://minio.elastic-mirror.svc:9000/elastic-mirror \
  -b https://minio.elastic-mirror.svc:9000/elastic-kb \
  -c mirror-ca.crt
```

Expect `ok` on every line. Do not continue on a `FAIL`. Then prove the path from inside Kibana, because a Kibana that cannot reach the registry loses its integration catalogue:

```sh
kubectl -n elastic-system exec deploy/<kibana>-kb -c kibana -- \
  curl -sS -m 10 -k -o /dev/null -w '%{http_code}\n' https://epr.elastic-mirror.svc:8080/
```

`200` is the pass. A timeout means a network policy or firewall rule is missing.

## 5. Point the existing ECK stack at the mirror

Keep a copy of the current `Kibana` and `Elasticsearch` resources first. That copy is the rollback.

1. Copy the CA into the ECK namespace.

   ```sh
   kubectl -n elastic-system create configmap elastic-mirror-ca --from-file=ca.crt=mirror-ca.crt
   ```

2. Kibana: merge `eck/kibana.yaml` into your `Kibana` resource. It sets:

   ```yaml
   xpack.fleet.isAirGapped: true
   xpack.fleet.registryUrl: "https://epr.elastic-mirror.svc:8080"
   xpack.productDocBase.artifactRepositoryUrl: "https://minio.elastic-mirror.svc:9000/elastic-kb"
   ```

   and `NODE_EXTRA_CA_CERTS` to the mounted CA. Kibana restarts.

3. Elasticsearch: merge `eck/elasticsearch.yaml` into every nodeSet, ML nodes included. It sets `ingest.geoip.downloader.endpoint` and `xpack.ml.model_repository`, and an init container that adds the CA to a copy of the JDK trust store. Both settings are static, so ECK rolls the nodes one at a time.
4. Wait for `GET _cluster/health` to report green. Do not trust the ECK resource status: it lags the cluster.
5. Check: Fleet, Integrations lists the catalogue, and `GET _ingest/geoip/stats` shows 3 databases and `failed_downloads: 0`.

If your trust bundle holds more than one certificate, `keytool -importcert` imports only the first. Split the PEM and import each, or GeoIP and model downloads fail with a TLS error while Kibana works.

## 6. Install the content

1. Integrations: Fleet, Integrations, install `Fleet Server`, `Elastic Defend`, `System` and the rest.
2. Detection rules: Security, Rules, Add Elastic rules, Install all. This installs `security_detection_engine` from your registry.
3. ELSER: Machine Learning, Trained models, `.elser_model_2_linux-x86_64`, Download, then Start deployment. It downloads from `models/`.
4. Elastic documentation for the AI Assistant: Stack Management, AI Assistants, Settings, install Elastic documentation. Kibana reads the bucket listing of `elastic-kb` and installs the product zips for your `<major.minor>`.
5. Security Labs: Security, AI Assistant, Settings, Knowledge base, enable it. The Security Labs content comes from the same bucket.
6. Agents, after they are enrolled: Fleet, Settings, Agent binary download, add `https://elastic-mirror.example.internal/elastic-mirror/downloads/` and tick default. Give agents the CA at install with `--certificate-authorities`. For upgrades, set the Fleet Server option `server.pgp.upstream_url: https://elastic-mirror.example.internal/elastic-mirror/GPG-KEY-elastic-agent`.
7. Defend, per policy, Advanced settings: `linux`, `windows` and `mac` `.advanced.artifacts.global.base_url` set to `https://elastic-mirror.example.internal/elastic-mirror`.

## Behaviour

- Rules and integrations change only with the registry image. To update: export `COMPONENTS=images EPR_VARIANT=production`, load it, change the digest in `k8s/epr.yaml`. The old pod serves until the new one has indexed.
- Defend artifacts change daily, GeoIP expires after 30 days without an update. Export `COMPONENTS="endpoint geoip"` at least monthly and run step 3.6 again. Objects are overwritten in place and nothing restarts.
- Documentation: export `COMPONENTS=kb` on each stack upgrade.
- `load-bundle.sh` stops on the first checksum mismatch and loads nothing.

## Out of scope

- The AI Assistant still needs an LLM. Point a connector at a model you host. Elastic-hosted inference needs Elastic Cloud.
- Licences: apply the licence file while connected or through your Elastic account team.
- UI links to the blog, release notes and Security Labs site do not open.

## Expected result

Namespace `elastic-mirror` with `epr` and `minio` pods Ready and no internet egress. Kibana lists every integration, Elastic rules install, the AI Assistant answers with documentation sources, and this passes:

```sh
curl -sS --cacert mirror-ca.crt "https://epr.elastic-mirror.svc:8080/search?package=security_detection_engine&kibana.version=$V" | jq -r '.[0].version'
```

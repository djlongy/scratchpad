# ECK air-gap mirror

## TL;DR

A Helm chart that serves the full Elastic package registry and every file an offline ECK stack fetches: integrations, detection rules, agent binaries, Defend artifacts, GeoIP, ELSER and the AI Assistant documentation. It builds on the OIDC setup in `docs/eck-keycloak-oidc`. On the air-gapped side you use `helm` and `kubectl` only.

For operator-managed EPR, use [the ECK manifests](eck/README.md).

## Requirements

- **Starting point:** the stack from `docs/eck-keycloak-oidc`, with the same placeholders (`<NS>`, `<ES>`, `<KB>`, `<INT_ISSUER>`).
  - cert-manager issues every internal certificate from the ClusterIssuer `<INT_ISSUER>`, which the corp CA signs.
  - Secret `corp-ca` in `<NS>`, key `ca.crt`, holds the corp CA bundle.
- **Connected host:** `bash`, `curl`, `jq`, `python3` 3.9 or later, `unzip`, about 2 GiB free disk.
- **Air-gapped cluster:**
  - an ingress-nginx controller, a `NetworkPolicy`-enforcing CNI and an internal image registry;
  - DNS names for the two mirror hosts in your zone, pointing at the ingress controller;
  - `helm` 3 and `kubectl`.
- **Registry node:** one node with at least 80 GiB of image disk for the registry pod (25 GiB compressed image plus 45 GiB extracted).
- **ML capacity:** an ML-capable Elasticsearch node and a Platinum, Enterprise or trial licence for ELSER and the AI Assistant documentation.

## Values

Full set: `chart/elastic-mirror/values.yaml`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `epr.image` | none | Registry image in your registry, by digest |
| Required | `files.image` | none | `nginx-unprivileged` Alpine image in your registry, by digest |
| Required | `ingress.clusterIssuer` | none | `<INT_ISSUER>`: issues both Ingress certificates |
| Required | `ingress.eprHost`, `ingress.filesHost` | none | Corp DNS names of the registry and the file server |
| Required | `ingress.controllerNamespace`, `ingress.controllerPodLabels` | `ingress-nginx`, `app.kubernetes.io/name: ingress-nginx` | The controller pods, the only clients the NetworkPolicy admits |
| Optional | `imagePullSecrets` | `[]` | Pull secrets for both pods |
| Optional | `epr.nodeSelector`, `epr.tolerations` | none | Put the registry on a node with a large image disk |
| Optional | `files.storageClassName`, `files.size` | default class, `20Gi` | The volume that holds the files |

## 1. Export on the connected host

1. Check the sizes. This downloads nothing.

   ```sh
   COMPONENTS="agent endpoint geoip models kb" MODELS="elser_model_2:linux-x86_64,generic" \
     scripts/mirror-bundle.sh -v 9.5.2 -o ./bundle-9.5.2 --dry-run
   ```

2. Export. Measured for 9.5.2, linux x86_64: 77 files, 1.3 GiB.

   ```sh
   COMPONENTS="agent endpoint geoip models kb" MODELS="elser_model_2:linux-x86_64,generic" \
     scripts/mirror-bundle.sh -v 9.5.2 -o ./bundle-9.5.2
   (cd bundle-9.5.2 && sha256sum -c --quiet MANIFEST.sha256)
   ```

   Add agent platforms with `AGENT_ARCHS="linux-x86_64 windows-x86_64"` and `AGENT_PACKAGES="tar.gz msi"`.

3. Bring these images into your internal registry through your site's normal image import, and note each digest:

   | Image | Size |
   |---|---|
   | `docker.elastic.co/package-registry/distribution:production` | about 25 GiB |
   | `docker.io/nginxinc/nginx-unprivileged:1.27-alpine` | about 20 MiB |

   `production` is the full catalogue with the newest rules. `distribution:9.5.2` is the full catalogue as released with 9.5.2, so its rules are older. `lite-9.5.2` holds about 17 packages and is not enough.

4. Carry `bundle-9.5.2/` and this folder's `chart/` and `eck/` across. Record `sha256sum bundle-9.5.2/MANIFEST.sha256` on the transfer paperwork.

## 2. Install the mirror

1. Write `values.yaml`:

   ```yaml
   imagePullSecrets: [{name: registry-pull}]
   epr:
     image: registry.example.internal/elastic/package-registry/distribution:production@sha256:<digest>
     nodeSelector: {kubernetes.io/hostname: <large-disk-node>}
   files:
     image: registry.example.internal/nginxinc/nginx-unprivileged:1.27-alpine@sha256:<digest>
   ingress:
     clusterIssuer: <INT_ISSUER>
     eprHost: epr.example.internal
     filesHost: elastic-mirror.example.internal
     controllerNamespace: ingress-nginx
     controllerPodLabels: {app.kubernetes.io/name: ingress-nginx}
   ```

2. Install, then wait. A cold pull of the registry image is the slow part. Its startup probe allows 30 minutes for indexing.

   ```sh
   kubectl create namespace elastic-mirror
   kubectl -n elastic-mirror create secret docker-registry registry-pull \
     --docker-server=registry.example.internal --docker-username=<puller> --docker-password=<token>
   helm install elastic-mirror chart/elastic-mirror -n elastic-mirror -f values.yaml
   kubectl -n elastic-mirror wait --for=condition=Ready certificate --all --timeout=5m
   kubectl -n elastic-mirror rollout status deploy/epr --timeout=60m
   kubectl -n elastic-mirror rollout status deploy/files --timeout=10m
   ```

3. Load the files and check every one against the manifest. The load is a tar stream through `kubectl exec` (1.3 GiB takes seconds on a LAN).

   ```sh
   tar -C ./bundle-9.5.2 -cf - MANIFEST.sha256 artifacts geoip models kb \
     | kubectl -n elastic-mirror exec -i deploy/files -- tar -xf - -C /srv/mirror
   kubectl -n elastic-mirror exec deploy/files -- \
     sh -c 'cd /srv/mirror && grep -v bundle.env MANIFEST.sha256 | sha256sum -c -s && echo all-files-match'
   ```

   Expect `all-files-match`. The files stay on the volume across pod restarts and `helm upgrade`.

## 3. Point the ECK stack at the mirror

Save the current resources first: `kubectl -n <NS> get kibana,elasticsearch -o yaml > before.yaml`. That file is the rollback.

1. Merge `eck/elasticsearch.yaml` into every nodeSet of `<ES>` and `eck/kibana.yaml` into `<KB>`. Set the two host names. The `corp-ca` volume and mount are the ones the OIDC setup already has. The fragments add:

   | Resource | Setting | Value |
   |---|---|---|
   | Kibana | `xpack.fleet.isAirGapped` | `true` |
   | Kibana | `xpack.fleet.registryUrl` | `https://epr.example.internal` |
   | Kibana | `xpack.productDocBase.artifactRepositoryUrl` | `https://elastic-mirror.example.internal/kb` |
   | Kibana | `NODE_EXTRA_CA_CERTS` | the mounted `corp-ca` |
   | Elasticsearch | `ingest.geoip.downloader.endpoint` | `https://elastic-mirror.example.internal/geoip/overview.json` |
   | Elasticsearch | `xpack.ml.model_repository` | `https://elastic-mirror.example.internal/models` |
   | Elasticsearch | init container `corp-trust-store` | the JVM trust store with every `corp-ca` certificate. GeoIP and the model repository use the JVM trust store, not the realm `ssl` settings |

2. Apply both with `kubectl apply -f`. Elasticsearch rolls node by node, and Kibana restarts. Wait for green: `kubectl -n <NS> get elasticsearch,kibana`.

## 4. Check from inside Kibana

```sh
KB="kubectl -n <NS> exec deploy/<KB>-kb -c kibana --"
CA=/usr/share/kibana/config/corp-ca/ca.crt
$KB curl -sS --cacert $CA "https://epr.example.internal/search?kibana.version=9.5.2" | jq length
$KB curl -sSI --cacert $CA https://elastic-mirror.example.internal/downloads/endpoint/manifest/artifacts-9.5.2.zip
$KB curl -sS --cacert $CA https://elastic-mirror.example.internal/kb | head -c 60
kubectl -n <NS> logs <ES>-es-default-0 -c corp-trust-store
```

Expect:
- a package count above 400;
- `200` with one `etag` and no dash in it;
- `<?xml ... <ListBucketResult`;
- one `Certificate was added to keystore` line per certificate in `corp-ca`.

A timeout means a NetworkPolicy or firewall rule between the stack and the ingress controller. A TLS error means `corp-ca` does not hold the CA that signs `<INT_ISSUER>`.

## 5. Install the content

Run these in Kibana once Kibana is green:

1. Integrations: Fleet, Integrations, install `Fleet Server`, `Elastic Defend`, `System` and any others you need.
2. Detection rules: Security, Rules, Add Elastic rules, Install all.
3. GeoIP: Dev Tools, `PUT _cluster/settings {"persistent":{"ingest.geoip.downloader.eager.download":true}}`, then `GET _ingest/geoip/stats` shows 3 databases and `failed_downloads: 0`.
4. ELSER: Machine Learning, Trained models, `.elser_model_2_linux-x86_64`, Download.
5. Elastic documentation for the AI Assistant: Stack Management, AI Assistants, Settings, install Elastic documentation.
6. Security AI Assistant knowledge base: Security, AI Assistant, Settings, Knowledge base, Setup.
   - Kibana 9.5.2 loads the 191 Security Labs documents from content bundled inside Kibana, not from the mirror, and embeds each one with ELSER.
   - Size the ML node for it. A single 8 GiB node with ML at 60 percent was OOMKilled. A single 12 GiB node with 2 CPUs indexed 190 of 191 after one bulk request timed out.
7. Agents: Fleet, Settings, Agent binary download, add `https://elastic-mirror.example.internal/downloads/` as default. Agents need the corp CA (`--certificate-authorities`).
8. Defend: in each Defend integration policy, Advanced settings, set `linux.advanced.artifacts.global.base_url`, `windows...` and `mac...` to `https://elastic-mirror.example.internal`.

## Behaviour

- **No egress:** the mirror pods serve plain HTTP and have no egress. Their NetworkPolicy admits only the ingress controller. TLS for every client ends at the Ingress with certificates from `<INT_ISSUER>`.
- **Rules and integrations:** these change only with the registry image. Import a newer `production` image, set its digest in `values.yaml` and run `helm upgrade`. The old pod serves until the new one has indexed.
- **Defend artifacts and GeoIP:** Defend artifacts change daily, and GeoIP databases expire after 30 days without an update. Export `COMPONENTS="endpoint geoip"` at least monthly and repeat step 2.3. No restart is needed.
- **Documentation:** export `COMPONENTS=kb` again on each stack upgrade.
- **Rollback:** `kubectl apply -f before.yaml` for the stack, and `helm rollback elastic-mirror` for the mirror.

## Out of scope

- **LLM:** the AI Assistant still needs one. Point a connector at a model you host.
- **OpenAPI documentation:** Kibana 9.5.2 rejects the upstream `kb-product-doc-openapi-9.5.zip` ("Manifest file not found"), so the export leaves it out.
- **Licence:** apply the licence file while connected or through your Elastic account team.

## Expected result

Validated on RKE2 with ECK 3.5 and Elastic 9.5.2. The stack's egress was limited to DNS, the ingress controller and Keycloak, the certificates came from a Vault-backed ClusterIssuer, and the trust came from a two-certificate `corp-ca` Secret.

- **Trust:** the OIDC realm loaded both `corp-ca` certificates and `oidc/prepare` returned the Keycloak redirect. The JVM trust store took 2 certificates.
- **Mirror:** pods `epr` and `files` are Ready in `elastic-mirror`. The registry lists 495 packages.
- **Content:** Elastic rules install (2316 rules). GeoIP downloads 3 databases with no failures. ELSER downloads from `models/`. The Elastic documentation installs for security, kibana, observability and elasticsearch.
- **Agent path:** from outside the cluster, with only the corp CA, the agent tarball passes `sha512sum -c` and `gpg --verify`, and all 49 Defend artifacts match their manifest hashes.

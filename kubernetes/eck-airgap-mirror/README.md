# ECK air-gap mirror

## TL;DR

A Helm chart that serves the full Elastic package registry and every file an offline ECK stack fetches: integrations, detection rules, agent binaries, Defend artifacts, GeoIP, ELSER and the AI Assistant documentation. On the air-gapped side you use `helm` and `kubectl` only.

## Requirements

- Connected host: `bash`, `curl`, `jq`, `python3` 3.9 or later, `unzip`, about 2 GiB free disk.
- Air-gapped cluster: Kubernetes with an enforcing `NetworkPolicy` CNI, cert-manager, an internal image registry, ECK running Elastic Stack 9.5.x, `helm` 3, `kubectl`.
- One node with at least 80 GiB of image disk for the registry pod (25 GiB compressed image plus 45 GiB extracted).
- An ML-capable Elasticsearch node and a Platinum, Enterprise or trial licence for ELSER and the AI Assistant documentation.

## Values

Full set: `chart/elastic-mirror/values.yaml`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `epr.image` | none | Registry image in your registry, by digest |
| Required | `files.image` | none | `nginx-unprivileged` Alpine image in your registry, by digest |
| Required | `consumerNamespaces` | `[elastic-system]` | Namespaces of the Kibana and Elasticsearch pods that read the mirror |
| Optional | `imagePullSecrets` | `[]` | Pull secrets for both pods |
| Optional | `epr.nodeSelector`, `epr.tolerations` | none | Put the registry on a node with a large image disk |
| Optional | `files.storageClassName`, `files.size` | default class, `20Gi` | The volume that holds the files |
| When agents run outside the cluster | `ingress.enabled`, `ingress.host`, `ingress.clusterIssuer`, `ingress.controllerNamespace`, `ingress.controllerPodLabels` | off | Ingress to the file server, and the NetworkPolicy rule for the controller pods |

## 1. Export on the connected host

1. Check the sizes. This downloads nothing.

   ```sh
   COMPONENTS="agent endpoint geoip models kb" MODELS="elser_model_2:linux-x86_64,generic" \
     scripts/mirror-bundle.sh -v 9.5.2 -o ./bundle-9.5.2 --dry-run
   ```

2. Export. Measured for 9.5.2, linux x86_64: 78 files, 1.3 GiB.

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

1. Write `values.yaml` with the required rows:

   ```yaml
   imagePullSecrets: [{name: registry-pull}]
   epr:
     image: registry.example.internal/elastic/package-registry/distribution:production@sha256:<digest>
     nodeSelector: {kubernetes.io/hostname: <large-disk-node>}
   files:
     image: registry.example.internal/nginxinc/nginx-unprivileged:1.27-alpine@sha256:<digest>
   consumerNamespaces: [elastic-system]
   ingress:
     enabled: true
     host: elastic-mirror.example.internal
     clusterIssuer: <your-cluster-issuer>
     controllerNamespace: ingress-nginx
     controllerPodLabels: {app.kubernetes.io/name: ingress-nginx}
   ```

2. Install, then wait. A cold pull of the registry image is the slow part. Its startup probe allows 30 minutes for indexing.

   ```sh
   kubectl create namespace elastic-mirror
   kubectl -n elastic-mirror create secret docker-registry registry-pull \
     --docker-server=registry.example.internal --docker-username=<puller> --docker-password=<token>
   helm install elastic-mirror chart/elastic-mirror -n elastic-mirror -f values.yaml
   kubectl -n elastic-mirror rollout status deploy/epr --timeout=60m
   kubectl -n elastic-mirror rollout status deploy/files --timeout=10m
   ```

3. Load the files and check every one against the manifest. The load is a tar stream through `kubectl exec` (1.3 GiB took 7 seconds on a LAN).

   ```sh
   tar -C ./bundle-9.5.2 -cf - MANIFEST.sha256 artifacts geoip models kb \
     | kubectl -n elastic-mirror exec -i deploy/files -- tar -xf - -C /srv/mirror
   kubectl -n elastic-mirror exec deploy/files -- \
     sh -c 'cd /srv/mirror && grep -v bundle.env MANIFEST.sha256 | sha256sum -c -s && echo all-files-match'
   ```

   Expect `all-files-match`. The files stay on the volume across pod restarts and `helm upgrade`.

## 3. Point the ECK stack at the mirror

Save your current `Kibana` and `Elasticsearch` resources first: `kubectl -n elastic-system get kibana,elasticsearch -o yaml > before.yaml`. That file is the rollback.

1. Copy the mirror CA into the stack namespace.

   ```sh
   kubectl -n elastic-mirror get secret elastic-mirror-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > mirror-ca.crt
   kubectl -n elastic-system create configmap elastic-mirror-ca --from-file=ca.crt=mirror-ca.crt
   ```

2. Merge `eck/elasticsearch.yaml` into every nodeSet of your `Elasticsearch` resource and `eck/kibana.yaml` into your `Kibana` resource. Replace `elastic-mirror` in the URLs if you installed into another namespace. Apply both with `kubectl apply -f`. Elasticsearch rolls node by node, and Kibana restarts.

   | Resource | Setting | Value |
   |---|---|---|
   | Kibana | `xpack.fleet.isAirGapped` | `true` |
   | Kibana | `xpack.fleet.registryUrl` | `https://epr.elastic-mirror.svc:8080` |
   | Kibana | `xpack.productDocBase.artifactRepositoryUrl` | `https://files.elastic-mirror.svc:8443/kb` |
   | Elasticsearch | `ingest.geoip.downloader.endpoint` | `https://files.elastic-mirror.svc:8443/geoip/overview.json` |
   | Elasticsearch | `xpack.ml.model_repository` | `https://files.elastic-mirror.svc:8443/models` |

3. Wait for green: `kubectl -n elastic-system get elasticsearch,kibana`.

## 4. Check from inside Kibana

```sh
KB="kubectl -n elastic-system exec deploy/<kibana>-kb -c kibana --"
CA=/usr/share/kibana/mirror-ca/ca.crt
$KB curl -sS --cacert $CA "https://epr.elastic-mirror.svc:8080/search?kibana.version=9.5.2" | jq length
$KB curl -sSI --cacert $CA https://files.elastic-mirror.svc:8443/downloads/endpoint/manifest/artifacts-9.5.2.zip
$KB curl -sS --cacert $CA https://files.elastic-mirror.svc:8443/kb | head -c 60
```

Expect a package count above 400, `HTTP/1.1 200` with one `ETag` and no dash in it, and `<?xml ... <ListBucketResult`. A timeout means a NetworkPolicy or firewall rule. A TLS error means the CA.

## 5. Install the content

Run these in Kibana once Kibana is green:

1. Integrations: Fleet, Integrations, install `Fleet Server`, `Elastic Defend`, `System` and any others you need.
2. Detection rules: Security, Rules, Add Elastic rules, Install all.
3. GeoIP: Dev Tools, `PUT _cluster/settings {"persistent":{"ingest.geoip.downloader.eager.download":true}}`, then `GET _ingest/geoip/stats` shows 3 databases and `failed_downloads: 0`.
4. ELSER: Machine Learning, Trained models, `.elser_model_2_linux-x86_64`, Download.
5. Elastic documentation for the AI Assistant: Stack Management, AI Assistants, Settings, install Elastic documentation.
6. Security AI Assistant knowledge base: Security, AI Assistant, Settings, Knowledge base, Setup. Kibana 9.5.2 loads the 191 Security Labs documents from content bundled inside Kibana, not from the mirror, and embeds each with ELSER. Size the ML node for it: a single 8 GiB node with ML at 60 percent was OOMKilled, and a single 12 GiB node with 2 CPUs indexed 190 of 191 after one bulk request timed out.
7. Agents: Fleet, Settings, Agent binary download, add `https://elastic-mirror.example.internal/downloads/` as default. Enrol agents with `--certificate-authorities` for your ingress CA.
8. Defend: in each Defend integration policy, Advanced settings, set `linux.advanced.artifacts.global.base_url`, `windows...` and `mac...` to `https://elastic-mirror.example.internal`.

## Behaviour

- **No egress:** the chart's NetworkPolicy denies all egress from the mirror and admits only the consumer namespaces and, with the Ingress on, the controller pods.
- **Rules and integrations:** these change only with the registry image. Import a newer `production` image, set its digest in `values.yaml` and run `helm upgrade`. The old pod serves until the new one has indexed.
- **Defend artifacts and GeoIP:** Defend artifacts change daily, and GeoIP databases expire after 30 days without an update. Export `COMPONENTS="endpoint geoip"` at least monthly and repeat step 2.3. No restart is needed.
- **Documentation:** export `COMPONENTS=kb` again on each stack upgrade.
- **Rollback:** `kubectl apply -f before.yaml` for the stack, and `helm rollback elastic-mirror` for the mirror.

## Out of scope

- **LLM:** the AI Assistant still needs one. Point a connector at a model you host.
- **OpenAPI documentation:** Kibana 9.5.2 rejects the upstream `kb-product-doc-openapi-9.5.zip` ("Manifest file not found"), so the export leaves it out.
- **Licence:** apply the licence file while connected or through your Elastic account team.

## Expected result

Validated on RKE2 with ECK 3.5 and Elastic 9.5.2, with the stack's egress limited to the mirror and DNS:

- **Mirror:** pods `epr` and `files` are Ready in `elastic-mirror`. The registry lists 495 packages and `security_detection_engine` 9.5.11.
- **Content:** Elastic rules install (2316 rules). GeoIP downloads 3 databases with no failures. ELSER downloads from `models/`. The Elastic documentation installs for security, kibana, observability and elasticsearch.
- **Security Labs:** 190 of 191 documents loaded on one 2 CPU node.
- **Agent path:** the agent tarball through the Ingress passes `sha512sum -c` and `gpg --verify`. All 49 Defend artifacts match their manifest hashes.

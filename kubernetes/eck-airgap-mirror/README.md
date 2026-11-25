# ECK air-gap mirror

## TL;DR

ECK manages the offline package registry; a separate HTTPS file mirror serves the remaining Elastic artifacts.

## Requirements

- ECK 3.5 with the `PackageRegistry` CRD, an existing Elastic 9.5.2 stack and `kubectl`.
- An internal image registry, its pull credentials and a NetworkPolicy-enforcing CNI.
- Eligible EPR nodes with at least 80 GiB of image disk for the full catalogue.
- A separate HTTPS file server with persistent storage, a certificate trusted by your stack and access from Elasticsearch, Kibana and agents.
- On the connected export host: Bash, curl, jq, Python 3.9 or later, unzip and SHA-256 tools. Image export also requires skopeo.

## Minimum configuration

Use [eck/package-registry.yaml](eck/package-registry.yaml), [eck/package-registry-networkpolicy.yaml](eck/package-registry-networkpolicy.yaml) and [eck/kibana-package-registry.yaml](eck/kibana-package-registry.yaml).

Replace the registry image with your imported full production image, pinned by digest. Set the namespace, Kibana name and pull-secret reference for your stack. The examples use namespace `elastic`, registry `registry` and Kibana `kb`.

The EPR policy permits only that Kibana's pods in the same namespace and grants no egress. Other policies are additive: ensure none grant unwanted access.

## Usage

1. On the connected host, export the artifact files and verify their manifest:

   ```sh
   COMPONENTS="agent endpoint geoip models kb" MODELS="elser_model_2:linux-x86_64,generic" \
     scripts/mirror-bundle.sh -v 9.5.2 -o ./bundle-9.5.2
   (cd bundle-9.5.2 && sha256sum -c MANIFEST.sha256)
   ```

2. Import the full `docker.elastic.co/package-registry/distribution:production` image through your approved image-transfer process. Record its digest in `eck/package-registry.yaml` and pre-stage it on eligible nodes. Carry the bundle and `eck/` manifests into the disconnected environment.
3. Create namespace `elastic` and its `registry-pull` Secret using your internal registry credentials. Apply the native resources:

   ```sh
   kubectl apply -f eck/package-registry-networkpolicy.yaml -f eck/package-registry.yaml
   kubectl -n elastic rollout status deployment/registry-epr --timeout=60m
   ```

4. Merge `eck/kibana-package-registry.yaml` into your existing Kibana resource and apply it. Remove an explicit `xpack.fleet.registryUrl`; `spec.packageRegistryRef` lets ECK configure the service URL and registry CA trust. Preserve your Elasticsearch association, authentication and other existing settings.
5. Publish the exported artifact files through your separate file server. Serve the contents of `artifacts/` at its root, and `geoip/`, `models/` and `kb/` at matching URL paths. Verify transferred files against `MANIFEST.sha256`. The `/kb` endpoint must provide the S3-style `ListBucketResult` XML index; Endpoint artifacts need an ETag without a dash.
6. Merge the file-mirror settings from `eck/elasticsearch.yaml` into each existing Elasticsearch nodeSet. From `eck/kibana.yaml`, take the documentation URL and file-mirror CA mount only: its explicit registry URL is for the chart deployment. If you set `NODE_EXTRA_CA_CERTS`, the referenced PEM bundle must trust both the file server and EPR; do not overwrite ECK's registry trust with a corp-only bundle. Apply the merged resources and wait for their rollouts.
7. In Fleet, install your integrations and set the agent binary download source to the file server's `/downloads/` URL. Set Defend's platform-specific `advanced.artifacts.global.base_url` values to the file-server root. Install detection rules, GeoIP, ELSER and product documentation as required by your licence and ML capacity.

## Expected result

```sh
kubectl -n elastic get packageregistry registry
kubectl -n elastic get kibana kb -o jsonpath='{.status.packageRegistryAssociationStatus}'
```

The registry is healthy and the association is `Established`. Verify a package install through Kibana; association status alone does not prove package downloads work.

The native path passed a simulated air-gap test on RKE2 with Elastic 9.5.2 and a 3.5-based operator build: EPR listed 495 packages, Kibana installed System 3.5.0, injected CA trust passed, and public egress and unauthorized registry ingress were blocked. Images were pre-staged; this is not a physical-disconnection or stock-operator qualification. The separate file server is outside that test.

## Behaviour

ECK owns the registry Deployment, HTTPS Service and certificates. Its default distribution is Lite; the explicit full image supplies the larger catalogue. Updating the imported digest updates the available integrations and detection-rule packages.

The startup probe allows 30 minutes for indexing. Pre-stage the image so a cold pull does not exceed the generated Deployment's ten-minute progress deadline.

Refresh Endpoint artifacts and GeoIP independently of the EPR image; GeoIP databases expire after 30 days without updates. Refresh product documentation on stack upgrades. Roll back registry configuration through your deployment source; file rollback requires restoring previous artifact files.

## Out of scope

The [chart/elastic-mirror](chart/elastic-mirror) directory still contains the combined Helm implementation, including its own EPR Deployment and nginx file server. It is an alternative to the native path above, not a dependency. Do not install its EPR alongside the native registry. The native manifests do not provision a file server or migrate an existing deployment.

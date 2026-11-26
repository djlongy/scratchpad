# ECK air-gap mirror

Deploy the combined Elastic stack with [Kustomize](stack/README.md): Elasticsearch,
Kibana, Fleet Server, native EPR, artifact serving, TLS and OIDC.

## Requirements

ECK 3.5, cert-manager, ingress-nginx, persistent storage, an existing Keycloak client
and the site inputs listed in `stack/README.md`. Images and artifact files must be
available inside the disconnected environment.

## Usage

1. Set site values and supply the ignored secret inputs described in `stack/README.md`.
2. For a new disposable evaluation cluster, apply:

   ```sh
   kubectl apply -k stack/overlays/trial
   ```

3. Load your exported artifact bundle into the file-server PVC and verify its manifest
   using the commands in `stack/README.md`.
4. Open your configured Kibana hostname and choose Keycloak.

The evaluation trial belongs to the new Elasticsearch cluster. The shared ECK operator
licence stays unchanged. OIDC authentication uses Keycloak; authorization uses the
file-realm immutable-subject allowlist and role assignments in `stack/base/users_roles`.

## Expected result

Three Elasticsearch nodes, Kibana, Fleet Server, EPR and the HTTPS file server are
ready. System, Elastic Agent and Fleet Server packages and managed policies are
preconfigured. The custom infrastructure-administrator assignment includes inference
access; built-in superusers already have it.

## Artifact export

Keep `scripts/mirror-bundle.sh` for connected-side export if you do not already have
an artifact transfer process. `kubectl apply -k` deploys resources; it does not transfer
images, agent binaries, GeoIP databases, models or documentation across an air gap.

The [individual ECK fragments](eck/README.md) are references for an existing stack.
[Offline docs and Security Labs](offline-docs/README.md) installs the Agent Builder
documentation from files on a volume, with no web server or CA trust.
The combined deployment and its exact feature limits are documented in `stack/README.md`.

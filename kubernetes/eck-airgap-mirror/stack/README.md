# Elastic stack

## TL;DR

Deploy Elasticsearch, Kibana, Fleet Server, EPR, artifact serving, TLS and OIDC with Kustomize.

## Requirements

- ECK 3.5 CRDs/operator, cert-manager, ingress-nginx and an enforcing NetworkPolicy CNI.
- A default ReadWriteOnce StorageClass: three 40 GiB Elasticsearch claims and one 20 GiB artifact claim. Rook-Ceph RBD works; file-serving storage can also use a suitable NFS or CephFS claim.
- Capacity for three 8 GiB Elasticsearch pods, 2 GiB Kibana, 2 GiB EPR requests, 1 GiB Fleet Server and the file server. This example uses three voting/data nodes.
- An existing Keycloak confidential client, its client secret, corporate CA chain, DNS records and a corporate cert-manager ClusterIssuer for ingress certificates.
- A Platinum/Enterprise Elasticsearch licence or a fresh cluster trial for OIDC. The default file-authorized path does not require an ECK Enterprise operator trial.

## Minimum configuration

The complete resources are in this directory. Set site values before applying:

| Req | Input | Location |
|---|---|---|
| Required | Keycloak issuer/endpoints, client ID and callback/logout URLs | `base/elasticsearch.yaml` |
| Required | Kibana/Fleet/files hostnames and corporate ingress issuer | `base/ingress.yaml`, `base/kibana.yaml`, `base/files.yaml` |
| Required | Imported EPR image by digest; other pinned images available internally | `base/package-registry.yaml` and workload image fields |
| Required | Identity IP allowlist and ingress-controller pod labels | `base/networkpolicy.yaml` |
| Required | Authorized immutable OIDC subject IDs and role assignments | `base/users_roles` and `base/secrets/users` |

Keep the namespace as `elastic`, or update every namespace-bearing service URL, association CA path and certificate DNS name in your overlay as well as the Kustomize namespace.

Supply these ignored files from your secret manager:

- `base/secrets/ca.crt`: PEM corporate CA chain trusted by the Keycloak HTTPS client.
- `base/secrets/oidc.env`: `xpack.security.authc.realms.oidc.oidc1.rp.client_secret=<client-secret>`.
- `base/secrets/dockerconfig.json`: Kubernetes Docker config JSON for your internal image registry.
- `base/secrets/users`: file-realm `subject-id:bcrypt-hash` records for the exact immutable Keycloak OIDC `sub` values listed in `base/users_roles`. Use independent random lookup-account passwords, not the users' Keycloak passwords. The file realm also permits basic authentication with those independent passwords.

Use the actual `sub` claim for this OIDC client; do not substitute a mutable username. The default placeholders authorize no real user. File-realm identifiers cannot contain a colon; use a compatible immutable subject claim or directory-backed authorization for such providers.

## Usage

```sh
kubectl apply -k .
```

For a new disposable evaluation cluster, use `kubectl apply -k overlays/trial` instead. Its licence applies to the new Elasticsearch cluster, not the shared ECK operator. The observer annotation is a test-only workaround for affected ECK 3.5 builds; use an operator-managed licence in production.

Wait for the ECK resources and certificates to become healthy. Supply the exported artifact bundle to the file server's PVC:

```sh
tar -C /path/to/bundle-9.5.2 -cf - . \
  | kubectl -n elastic exec -i deploy/files -- tar -xf - -C /srv/mirror
kubectl -n elastic exec deploy/files -- sh -c 'cd /srv/mirror && sha256sum -c MANIFEST.sha256'
```

Pre-populating the claim through your storage/transfer process works too. Transfer completion is separate from Kubernetes resource readiness.

## Behaviour

Keycloak authenticates OIDC users. Elasticsearch delegates authorization to its file realm (`authorization_realms: [file1]`): `base/users_roles` is the immutable-subject allowlist and role assignment file. Custom permissions come from the shared `roles.yml` Secret generator. Keycloak group claims do not grant roles in this path; the Enterprise group-mapping policy in the OIDC guide is separate.

Fleet Server uses evaluation-only `emptyDir` state; pod replacement re-enrolls it. Use persistent Agent state for production. Monitoring uses the process runtime on the qualified 9.5.2 stack while keeping logs and metrics enabled; requalify the default runtime when upgrading. Fleet Server enrolls against the preconfigured `eck-fleet-server` policy. Kibana installs pinned System, Elastic Agent and Fleet Server packages from the native EPR; `eck-agent` provides the System monitoring policy. Kibana's OIDC login, basic break-glass provider and package registry association are preconfigured.

The namespace CA signs EPR and file-server certificates; ECK supplies their CA trust to Kibana. Elasticsearch's init container imports that CA and the corporate CA into a copy of the JDK truststore. Ingress certificates come from your corporate issuer. NetworkPolicy allows the namespace, DNS, the ECK operator, ingress and the configured identity address, while other external egress is denied.

## Expected result

`kubectl -n elastic get elasticsearch,kibana,agent,packageregistry,pods,pvc,certificate` shows healthy components, bound storage and ready certificates. Open the configured Kibana hostname and choose Keycloak; the authenticated OIDC subject ID must exist in `base/secrets/users` and `base/users_roles`.

## Out of scope

The manifest apply does not carry container images or artifact bytes across an air gap. Keep `../scripts/mirror-bundle.sh` for connected-side export, or supply equivalent mirrored content through your existing transfer system.

On Kibana 9.5.2, configure the agent binary download source in Fleet Settings after deployment; the newer declarative `binaryDownloadSource` setting is rejected by this version. Agent enrollment on external hosts, Endpoint policy setup, detection-rule activation, inference-provider connectors and product-documentation/model downloads require their own configuration and validation. Their presence in the UI is not evidence that they are ready.

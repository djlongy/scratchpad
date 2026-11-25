# Elasticsearch and Kibana on ECK with Keycloak OIDC

Log in to Kibana through Keycloak, with Elasticsearch roles taken from Keycloak (or
LDAP) group membership. Tested with ECK 3.5 and Elasticsearch/Kibana 9.5.

Replace these placeholders throughout:

| Placeholder | Example |
|---|---|
| `<KEYCLOAK>` | `https://sso.example.com` |
| `<REALM>` | `corp` |
| `<CLIENT_ID>` | `kibana` |
| `<KIBANA_URL>` | `https://kibana.example.com` (no trailing slash) |
| `<NS>` | `elastic` |
| `<ES>` | `es` (Elasticsearch resource name) |
| `<KB>` | `kb` (Kibana resource name) |
| `<INT_ISSUER>` | `corp-int` (cert-manager ClusterIssuer signed by the corp CA) |

Certificates: cert-manager issues every internal certificate from `<INT_ISSUER>`,
and the corp CA signs that issuer and Keycloak's certificate. Elasticsearch and Kibana
trust the corp CA through one Secret, `corp-ca` in `<NS>`, key `ca.crt`: a PEM bundle
of the corp root and intermediate certificates, each ending in a newline (Elasticsearch
refuses a bundle whose `-----END CERTIFICATE-----` line runs into the next one).

```bash
kubectl -n <NS> create secret generic corp-ca --from-file=ca.crt=corp-ca-bundle.pem
```

## 0. Licence first

The OIDC realm needs a **Platinum or Enterprise** licence, or a trial. On a Basic
licence the realm does not load and the Kibana button fails.

- Production: install your Enterprise licence for ECK (a Secret in the operator
  namespace with label `license.k8s.elastic.co/scope: operator`). It covers every
  cluster that operator manages.
- Evaluation: start a 30 day trial on the cluster (step 6). A cluster can start a trial
  only once.
- ECK 3.5 can reset a stack-level trial to Basic right after an Elasticsearch restart.
  Prevent it with the annotation `eck.k8s.elastic.co/es-observer-interval: "0s"` on the
  Elasticsearch resource, set **before** the trial starts (adding it to a running
  cluster opens the window once).

## 1. Keycloak client

Admin console, realm `<REALM>`, Clients, Create client:

| Setting | Value |
|---|---|
| Client type | OpenID Connect |
| Client ID | `<CLIENT_ID>` |
| Client authentication | **On** (confidential) |
| Standard flow | On. Direct access grants, implicit flow: Off |
| Root URL | `<KIBANA_URL>` |
| Valid redirect URIs | `<KIBANA_URL>/api/security/oidc/callback` |
| Valid post logout redirect URIs | `<KIBANA_URL>/security/logged_out` |
| Web origins | `<KIBANA_URL>` |

Credentials tab: copy the **Client secret**. Store it in your secret manager, not in
git.

Groups claim: Client scopes, `<CLIENT_ID>-dedicated`, Add mapper, By configuration,
**Group Membership**:

| Field | Value |
|---|---|
| Name / Token claim name | `groups` |
| Full group path | **Off** (emit `es-admins`, not `/es-admins`) |
| Add to ID token, access token, userinfo | On |

Get the endpoints for step 3 from
`<KEYCLOAK>/realms/<REALM>/.well-known/openid-configuration`.

If users and groups come from LDAP (User Federation, group mapper), only **direct**
memberships reach the token. A group nested inside another group does not, so put
users straight into the groups you map in step 7. After changing membership in LDAP,
Keycloak can serve cached groups until you clear the realm's user cache (Realm
settings, Action, Clear user cache) and the user logs in again.

## 2. Client secret into the Elasticsearch keystore

The Secret's key must be the full setting name. ECK loads it through
`spec.secureSettings`.

```bash
read -rs OIDC_SECRET            # paste the client secret, nothing is echoed
kubectl -n <NS> create secret generic es-oidc-secure-settings \
  --from-literal="xpack.security.authc.realms.oidc.oidc1.rp.client_secret=$OIDC_SECRET"
unset OIDC_SECRET
```

With External Secrets, produce the same Secret name and key from your store.

## 3. Elasticsearch

```yaml
apiVersion: elasticsearch.k8s.elastic.co/v1
kind: Elasticsearch
metadata:
  name: <ES>
  namespace: <NS>
  annotations:
    eck.k8s.elastic.co/es-observer-interval: "0s"   # keeps a trial across restarts (section 0)
spec:
  version: 9.5.2
  secureSettings:
    - secretName: es-oidc-secure-settings
  nodeSets:
    - name: default
      count: 3
      config:
        xpack.security.authc.realms.oidc.oidc1:
          order: 2                                  # after native and file realms, so "elastic" still works
          rp.client_id: <CLIENT_ID>
          rp.response_type: code
          rp.requested_scopes: ["openid", "profile", "email"]
          rp.redirect_uri: <KIBANA_URL>/api/security/oidc/callback
          rp.post_logout_redirect_uri: <KIBANA_URL>/security/logged_out
          op.issuer: <KEYCLOAK>/realms/<REALM>
          op.authorization_endpoint: <KEYCLOAK>/realms/<REALM>/protocol/openid-connect/auth
          op.token_endpoint: <KEYCLOAK>/realms/<REALM>/protocol/openid-connect/token
          op.userinfo_endpoint: <KEYCLOAK>/realms/<REALM>/protocol/openid-connect/userinfo
          op.endsession_endpoint: <KEYCLOAK>/realms/<REALM>/protocol/openid-connect/logout
          op.jwkset_path: <KEYCLOAK>/realms/<REALM>/protocol/openid-connect/certs
          claims.principal: preferred_username      # becomes the Elasticsearch user name
          claims.groups: groups                     # feeds the role mapping rules
          claims.mail: email
          claims.name: name
          ssl.certificate_authorities: ["/usr/share/elasticsearch/config/corp-ca/ca.crt"]
      podTemplate:
        spec:
          containers:
            - name: elasticsearch
              volumeMounts:
                - name: corp-ca
                  mountPath: /usr/share/elasticsearch/config/corp-ca
                  readOnly: true
          volumes:
            - name: corp-ca
              secret:
                secretName: corp-ca
      volumeClaimTemplates:
        - metadata:
            name: elasticsearch-data
          spec:
            accessModes: ["ReadWriteOnce"]
            resources:
              requests:
                storage: 50Gi
```

Notes:

- `op.issuer` must match `issuer` in the discovery document exactly.
- `rp.redirect_uri` must match the Keycloak redirect URI exactly, scheme and host
  included.
- Elasticsearch fetches `op.jwkset_path` and the token endpoint over HTTPS. The realm
  trusts Keycloak through `ssl.certificate_authorities`, read from the mounted
  `corp-ca` Secret. `GET _ssl/certificates` lists every certificate of the bundle under
  that path.

- Egress through a proxy: add `-Dhttps.proxyHost=... -Dhttps.proxyPort=...` to
  `ES_JAVA_OPTS`, or allow direct egress from the Elasticsearch pods to Keycloak.
- Realm settings are static: changing them, or the secureSettings Secret, restarts the
  Elasticsearch pods (rolling).

## 4. Kibana

```yaml
apiVersion: kibana.k8s.elastic.co/v1
kind: Kibana
metadata:
  name: <KB>
  namespace: <NS>
spec:
  version: 9.5.2
  count: 1
  elasticsearchRef:
    name: <ES>
  config:
    server.publicBaseUrl: <KIBANA_URL>      # must match the redirect URI host
    xpack.security.authc.providers:
      oidc.oidc1:
        order: 0
        realm: oidc1                         # the realm name from step 3
        description: "Log in with Keycloak"
      basic.basic1:
        order: 1                             # keep the login form for break-glass accounts
```

Drop `basic.basic1` later if every human should use SSO, but keep a way in for the
`elastic` user (port-forward to Kibana or Elasticsearch).

## 5. Ingress

ECK serves Kibana over TLS with its own certificate, so the ingress must talk HTTPS to
the backend.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: kibana
  namespace: <NS>
  annotations:
    cert-manager.io/cluster-issuer: <INT_ISSUER>            # issues kibana-tls
    nginx.ingress.kubernetes.io/backend-protocol: HTTPS
    nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"   # large OIDC cookies and headers
spec:
  ingressClassName: nginx
  tls:
    - hosts: [kibana.example.com]
      secretName: kibana-tls
  rules:
    - host: kibana.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: <KB>-kb-http
                port:
                  number: 5601
```

## 6. Apply and check

```bash
kubectl apply -f elasticsearch.yaml -f kibana.yaml -f ingress.yaml
kubectl -n <NS> get elasticsearch,kibana          # wait for HEALTH green

PW=$(kubectl -n <NS> get secret <ES>-es-elastic-user -o go-template='{{.data.elastic | base64decode}}')
kubectl -n <NS> port-forward svc/<ES>-es-http 9200 &
ES() { curl -sk -u "elastic:$PW" -H 'Content-Type: application/json' "https://localhost:9200$1" "${@:2}"; }

ES /_license                                       # type must be platinum, enterprise or trial
ES /_license/start_trial?acknowledge=true -X POST  # evaluation only, once per cluster
ES /_security/oidc/prepare -X POST -d '{"realm":"oidc1"}'   # must return a Keycloak "redirect" URL
```

`oidc/prepare` returning a redirect URL proves the realm loaded. An error here
(licence, bad issuer, missing secret) shows up before anyone tries the browser.

## 7. Define roles and group mappings as code

[roles.yml](roles.yml) defines the `logs-read` role and its permissions: read and
index-metadata access to `logs-*`, plus Kibana Discover and Dashboard read access.
The Kibana privileges apply to all spaces; restrict `resources` to named spaces if
required. Elastic supplies built-in roles such as `superuser`, `editor` and `viewer`.

Import the file into a Kubernetes Secret in `<NS>`:

```sh
kubectl -n <NS> create secret generic es-custom-roles --from-file=roles.yml=roles.yml \
  --dry-run=client -o yaml | kubectl apply -f -
```

Merge [elasticsearch-roles.yml](elasticsearch-roles.yml) into your existing Elasticsearch
manifest, preserving its other authentication settings, and apply the merged manifest.
ECK distributes the role file to every node through `spec.auth.roles`; Elasticsearch
reloads it. Repeat the Secret apply command after editing permissions. File-defined
roles cannot be viewed or edited through the role-management API or UI. Kibana must
start and register its application privileges before its feature permissions take effect.

### OIDC group mappings

[role-mappings.yml](role-mappings.yml) declares the group assignments as an ECK
`StackConfigPolicy`. This requires an **ECK Enterprise or Enterprise trial licence**,
separate from the stack licence needed by OIDC. Without it, ECK does not apply this
policy; a `Ready` status with `readyCount: 0/0` is not evidence that mappings exist.
The file-based role import above does not require that ECK licence.

Set the policy namespace to `<NS>` and add `security-profile: oidc` to the target
Elasticsearch resource's `metadata.labels` in its manifest. Apply the updated
Elasticsearch manifest and then `kubectl apply -f role-mappings.yml`. Use a workload
namespace, not the operator namespace, to keep its scope within that namespace.

The policy maps `es-admins`, `es-analysts`, `es-viewers` and `es-log-readers` to
`superuser`, `editor`, `viewer` and `logs-read`. Every mapping also requires realm
`oidc1`, so a same-named group from another realm cannot grant these roles.

Elasticsearch's `role_mapping.yml` files support PKI, LDAP and Active Directory,
not OIDC. The operator policy is the declarative OIDC mapping path here; no manual
role or role-mapping API writes are part of this procedure. A user who authenticates
but matches no mapping receives no roles. Verify the policy has applied to the target
cluster before testing login.

## 8. Verify a real login

1. Open `<KIBANA_URL>` and choose "Log in with Keycloak".
2. After login, check what Elasticsearch saw. As that user in Kibana Dev Tools:
   `GET _security/_authenticate`. Look at `username`, `authentication_realm.name`
   (`oidc1`), `metadata["oidc(groups)"]` and `roles`.
3. Missing roles: compare `metadata["oidc(groups)"]` with the group names in your rules.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Keycloak: "Invalid parameter: redirect_uri" | Keycloak redirect URI differs from `rp.redirect_uri` | Make both exactly `<KIBANA_URL>/api/security/oidc/callback` |
| Kibana: "Cannot find OIDC realm" or button errors | Licence is Basic, or realm failed to load | `GET _license`; read the Elasticsearch logs for `oidc1` |
| Login works, "no permissions" | No role mapping matched | Step 8; groups claim missing, or full path on (`/group`) |
| Groups claim empty for LDAP users | Membership is nested, or cached | Direct membership; clear Keycloak user cache |
| `PKIX path building failed` in ES logs | ES does not trust Keycloak's CA | `corp-ca` must hold the CA that signs Keycloak; `ssl.certificate_authorities` (step 3) |
| ES will not start: `Invalid PEM format: No EOL char found in footer` | `corp-ca` bundle joined without newlines | Put each `-----END CERTIFICATE-----` on its own line, recreate the Secret |
| Login loops or 502 at the ingress | Backend protocol HTTP, or header too large | `backend-protocol: HTTPS`; raise proxy buffer size |
| Trial gone after a restart | ECK reset it to Basic | Annotation in section 0, set at creation; the cluster cannot re-trial |

## What restarts what

| Change | Restarts Elasticsearch |
|---|---|
| Role files, role-mapping policy | No |
| Kibana `config` | Kibana only |
| Realm settings, secureSettings Secret | Yes, rolling |

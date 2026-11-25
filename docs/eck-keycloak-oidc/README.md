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

## 7. Map Keycloak groups to Elasticsearch roles

A user who logs in but matches no mapping gets no roles, and Kibana says they have no
access. Create mappings with the API (or Kibana: Stack Management, Security, Role
Mappings). ECK has no CRD for role mappings.

Three tiers, one Keycloak group each:

```bash
ES /_security/role_mapping/oidc-admins -X PUT -d '{
  "enabled": true,
  "roles": ["superuser"],
  "rules": { "all": [
    { "field": { "realm.name": "oidc1" } },
    { "field": { "groups": "es-admins" } } ] } }'

ES /_security/role_mapping/oidc-analysts -X PUT -d '{
  "enabled": true,
  "roles": ["editor"],
  "rules": { "all": [
    { "field": { "realm.name": "oidc1" } },
    { "field": { "groups": "es-analysts" } } ] } }'

ES /_security/role_mapping/oidc-viewers -X PUT -d '{
  "enabled": true,
  "roles": ["viewer"],
  "rules": { "all": [
    { "field": { "realm.name": "oidc1" } },
    { "field": { "groups": "es-viewers" } } ] } }'
```

Always include `realm.name` in the rule, so a group of the same name from another realm
cannot grant the role.

Useful rule shapes:

| Need | Rule |
|---|---|
| Any of several groups | `{"any": [{"field":{"groups":"team-a"}}, {"field":{"groups":"team-b"}}]}` |
| Group name pattern | `{"field": {"groups": "es-*"}}` |
| One named user | `{"field": {"username": "jane.doe"}}` |
| Exclude a group | `{"except": {"field": {"groups": "contractors"}}}` inside an `all` |

Role per group, without one mapping each (every group named like a role becomes that
role):

```json
{
  "enabled": true,
  "role_templates": [ { "template": { "source": "{{#tojson}}groups{{/tojson}}" }, "format": "json" } ],
  "rules": { "field": { "realm.name": "oidc1" } }
}
```

### Custom roles

Built-in roles: `superuser`, `editor`, `viewer`, plus `kibana_admin` and others. For
per-index access, define your own role and map a group to it:

```bash
ES /_security/role/logs-read -X PUT -d '{
  "indices": [ { "names": ["logs-*"], "privileges": ["read", "view_index_metadata"] } ],
  "applications": [ { "application": "kibana-.kibana",
                      "privileges": ["feature_discover.read", "feature_dashboard.read"],
                      "resources": ["*"] } ] }'
```

Or keep roles in git: a Secret with a `roles.yml` key, referenced from the
Elasticsearch spec (file realm roles, applied by ECK):

```yaml
spec:
  auth:
    roles:
      - secretName: es-custom-roles
```

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
| Role mappings, roles (API) | No |
| Kibana `config` | Kibana only |
| Realm settings, secureSettings Secret | Yes, rolling |

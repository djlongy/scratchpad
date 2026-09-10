# GitLab Pages on a self-hosted instance: enabling it, and getting a URL people remember

What an administrator has to switch on, what a project owner can do without the
administrator, and the ways to end up with a predictable address such as
`wiki.example.com`. Every option lists what it needs, who can do it, and what was tested.

Companion to the [Zensical / Material tutorial](../mkdocs-material/) and the
[wiki mirror](../gitlab-wiki-mirror/). Instance used for testing: GitLab EE 18.9 (Linux
package), Pages enabled on a wildcard subdomain, HTTP only, access control off.

## 1. How Pages addresses work

With Pages enabled on `pages.example.com`, every project gets a site at

```text
https://<group>.pages.example.com/<project>/        default
https://<project>-<hash>.pages.example.com/         "unique domain" (on by default since 17.x)
```

The unique domain exists so that two projects in one group cannot share cookies or
scripts. It is also why the default URL is unreadable. Turn it off per project under
*Deploy > Pages > "Use unique domain"* (or `PATCH /projects/:id/pages` with
`is_unique_domain_enabled=false`) to get the `<group>.pages.example.com/<project>/` form.

Sites are served under a path prefix, so `site_url` in `mkdocs.yml` must include it and
root-absolute links (`/section/page`) break. Relative links do not.

## 2. Enabling Pages (administrator, once)

Linux package (`/etc/gitlab/gitlab.rb`), then `gitlab-ctl reconfigure`:

```ruby
pages_external_url "https://pages.example.com/"
gitlab_pages['enable'] = true
gitlab_pages['access_control'] = true          # section 5; harmless to set now
pages_nginx['redirect_http_to_https'] = true
```

Requirements around it:

- **DNS**: a wildcard record `*.pages.example.com` pointing at the GitLab server (the same
  address as `gitlab.example.com` in the simple layout, where the bundled nginx serves both).
- **TLS**: a wildcard certificate for `*.pages.example.com` at
  `/etc/gitlab/ssl/pages.example.com.crt` and `.key`, or explicit paths in
  `pages_nginx['ssl_certificate']` / `['ssl_certificate_key']`. An internal CA that can issue
  wildcards is enough; the built-in Let's Encrypt integration only works for names the public
  internet can validate.
- **The Pages daemon's port**: bundled nginx proxies to `gitlab-pages` on `127.0.0.1:8090`;
  nothing to open on the firewall beyond 80/443 to the GitLab host.
- Pages is included in the Free tier on self-managed.

Source: [GitLab Pages administration](https://docs.gitlab.com/administration/pages/).

Verify: a project with a `pages` job (section 6) shows its URL under *Deploy > Pages*
within a minute of a green pipeline. From the instance used here:

```text
$ curl -s -o /dev/null -w '%{http_code}\n' http://docs-1a2b3c.pages.example.com/
200
```

## 3. The URL options

| # | Address you get | Who does it | Needs | Tested here |
|---|---|---|---|---|
| A | `https://<group>.pages.example.com/<project>/` | project owner | unique domain off | yes |
| B | `https://wiki.pages.example.com/` | project owner | a group named `wiki` holding a project named `wiki.pages.example.com` | yes |
| C | `https://wiki.example.com/` (Pages custom domain) | admin, then owner | second listener/IP on the Pages daemon, DNS record, verification TXT, a certificate | not testable on this instance |
| D | `https://wiki.example.com/` (reverse proxy in front) | whoever runs the proxy | any proxy that can rewrite a path prefix; DNS to the proxy | config below, not exercised |
| E | `https://wiki.example.com/` (Kubernetes ingress) | platform team | the [container deployment](../mkdocs-on-kubernetes/), no Pages at all | image + manifests tested |

### A. Turn off the unique domain

*Deploy > Pages*, untick "Use unique domain", or:

```bash
curl -X PATCH -H "PRIVATE-TOKEN: $TOKEN" "$API/projects/<id>/pages?is_unique_domain_enabled=false"
```

Set `site_url: https://<group>.pages.example.com/<project>/` in `mkdocs.yml`. Readable, but
still carries the group and project path.

### B. A namespace-root project (the no-admin way to a clean subdomain)

GitLab serves a project named exactly `<namespace>.<pages-domain>` at the root of that
namespace's subdomain instead of under a path. So:

```text
group    wiki
project  wiki.pages.example.com          (the project *name and path* are the FQDN)
result   https://wiki.pages.example.com/
```

Tested on the instance used here: created group `wiki`, project `wiki.pages.<domain>`, one
`pages` job publishing two HTML files:

```text
http://wiki.pages.<domain>/            -> 200   (index.html from the project)
http://wiki.pages.<domain>/sub.html    -> 200
http://wiki.pages.<domain>/wiki.pages.<domain>/  -> 404  (no path prefix any more)
```

The unique-domain flag is ignored for such a project (the API kept reporting it as on; the
URL was the root regardless). Nothing to configure on the server. The name is fixed by the
Pages domain, so you get `wiki.pages.example.com`, not `wiki.example.com`; with a short,
memorable Pages domain (`pages_external_url "https://docs.example.com/"` gives
`wiki.docs.example.com`) that is often good enough. `site_url` becomes the root URL and
root-absolute links work.

Move an existing docs repo there with *Settings > General > Advanced > Transfer project*
and rename it, or keep the docs repo where it is and let this project's pipeline pull
and build it; the first is simpler.

### C. A Pages custom domain

The real `wiki.example.com`, served by the Pages daemon itself. Two halves.

**Administrator** (`gitlab.rb`, reconfigure). The daemon needs its own listeners, which
must not collide with the bundled nginx on 80/443, so in practice a second IP address on the
GitLab host:

```ruby
pages_nginx['enable'] = false
gitlab_pages['external_http']  = ['192.0.2.2:80']
gitlab_pages['external_https'] = ['192.0.2.2:443']
gitlab_pages['custom_domain_mode'] = 'https'
gitlab_pages['cert']     = "/etc/gitlab/ssl/pages.example.com.crt"   # wildcard, still needed
gitlab_pages['cert_key'] = "/etc/gitlab/ssl/pages.example.com.key"
```

Then the wildcard DNS `*.pages.example.com` and any custom names point at **that** address.
Source: [custom domains on the administration page](https://docs.gitlab.com/administration/pages/#custom-domains).

**Project owner** (*Deploy > Pages > New domain*): enter `wiki.example.com`, then create in
DNS a `CNAME wiki.example.com -> <group>.pages.example.com` (or an `A` record to the Pages IP
for an apex) and the verification record
`TXT _gitlab-pages-verification-code.wiki.example.com "gitlab-pages-verification-code=<code>"`
shown on that page. Certificate: upload one from the internal CA (PEM + key), or use the
Let's Encrypt toggle if the name is publicly resolvable. An administrator can switch domain
verification off instance-wide for internal DNS.
Source: [custom domains and TLS](https://docs.gitlab.com/user/project/pages/custom_domains_ssl_tls_certification/).

Not exercised on the instance used here (no second address on the Pages daemon). It is
the only option that gives the Pages daemon itself the short name, with access control
(section 5) still applying.

### D. A reverse proxy in front

If a proxy already fronts internal services (nginx, Caddy, Traefik, an ingress controller, a
UI-driven proxy manager), `wiki.example.com` can simply proxy to the Pages site. The one
thing to get right is the path prefix: either publish the site at a root (option B) so no
rewrite is needed, or strip and add the prefix.

nginx, proxying to a root-published site (option B) or a unique-domain site:

```nginx
server {
    listen 443 ssl;
    server_name wiki.example.com;
    ssl_certificate     /etc/ssl/wiki.example.com.crt;
    ssl_certificate_key /etc/ssl/wiki.example.com.key;

    location / {
        proxy_pass https://wiki.pages.example.com/;
        proxy_set_header Host wiki.pages.example.com;   # Pages routes on the Host header
        proxy_ssl_server_name on;
    }
}
```

For a path-prefixed site (`<group>.pages.example.com/<project>/`) add the prefix on the way in
and set `site_url` to `https://wiki.example.com/`, otherwise the site's own absolute links
send readers back to the Pages hostname:

```nginx
    location / {
        proxy_pass https://group.pages.example.com/project/;
        proxy_set_header Host group.pages.example.com;
        proxy_redirect https://group.pages.example.com/project/ /;
    }
```

Pages access control (section 5) does not survive a proxy cleanly (the OAuth redirect goes
to the Pages hostname), so put authentication on the proxy instead if the docs are internal.

### E. Kubernetes ingress

No Pages involved: the [container option](../mkdocs-on-kubernetes/) serves the site from a
pod, and the ingress gives it `wiki.example.com` with the cluster's certificates and
whatever auth the ingress offers. Choose this when a cluster already fronts internal apps.

## 4. Where the DNS lives

Any of the above works with any of these; pick the one that matches how names are managed:

- **Internal resolver only** (the usual case for an intranet): wildcard `*.pages.example.com`
  and `wiki.example.com` in the internal DNS; nothing public.
- **Split horizon**: the public zone has no record (or a different one); the internal view
  resolves to private addresses. Fine for options A–E.
- **Public DNS with private addresses**: records in the public zone pointing at RFC 1918
  addresses. Works, leaks hostnames, and makes Let's Encrypt DNS-01 possible for wildcards.
- **Public DNS and public reachability**: only if the docs are meant to be public; then Pages
  access control or proxy auth is mandatory.

## 5. Keeping internal docs internal

- `gitlab_pages['access_control'] = true` on the server, then per project *Deploy > Pages >
  "Only project members" / "Everyone with access"*. Readers log in through GitLab (an OAuth
  application the daemon registers for itself). Note from the test instance: with this off,
  a project's Pages marked "private" in the API still answered 200 with no login, so the
  server-side switch is what matters.
- Or authentication on the proxy/ingress (option D/E).
- Or network placement only (internal DNS, no route from outside), which is what most
  intranets rely on anyway.

## 6. The job that publishes

GitLab 17.9 and later (nested `pages:` keyword; Zensical writes to `site/`):

```yaml
deploy-docs:
  stage: deploy
  image: python:3.12-slim
  script:
    - pip install -r requirements.txt
    - zensical build --clean --strict
  pages:
    publish: site
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
```

Older instances: name the job `pages`, build into `public/`, and declare
`artifacts: paths: [public]`. Both forms are in the [tutorial](../mkdocs-material/).

## 7. Decision

- Pages already enabled, admin not available, want a clean name today: **B**
  (`wiki.pages.example.com`).
- Want exactly `wiki.example.com` and a proxy already exists: **D**.
- Want exactly `wiki.example.com` with Pages' own login: **C**, and budget an admin change.
- No Pages, have a cluster: **E**.

Whatever the choice, `docs/` in the repo stays the source; only `site_url` and the DNS
change.

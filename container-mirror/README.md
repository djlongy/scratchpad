# Container mirror pipeline

Copies a list of container images into your own registry, then packs the layers
the far side of an air gap does not have yet into one tar in object storage.

## 1. Import the templates

Create a GitLab project and push the contents of this folder to it. Keep the
paths: `pipelines/` and `templates/` must sit at the project root.

```bash
git clone <this repo> scratchpad
cp -R scratchpad/container-mirror ci-mirror-templates
cd ci-mirror-templates
git init -b main && git add -A && git commit -m 'Container mirror templates'
git remote add origin git@<your-gitlab>:platform/ci-mirror-templates.git
git push -u origin main
git tag -a 0.1.0 -m 'Release 0.1.0' && git push origin 0.1.0
```

Protect the tag under **Settings > Repository > Protected tags**, so the ref a
consumer pins cannot be moved.

## 2. Create the consumer project

A second project, with two files copied from `examples/consumer/`:

- `.gitlab-ci.yml` — the include. Change `project:` to the project from step 1,
  `ref:` to its tag, and the `registry`, `repository-prefix`, `s3-*` and
  `have-key` inputs to yours. On a registry that scopes an account to one
  organisation, such as Quay or Harbor, `repository-prefix` must start with
  that organisation or every push is rejected as unauthenticated.
- `images.txt` — one image reference per line.

That is the whole consumer. No stages, no scripts.

## 3. Set the CI/CD variables

**Settings > CI/CD > Variables**, in the consumer project:

| Name | Masked | What it is |
|---|---|---|
| `REGISTRY_USER` | if 8+ characters | Account that can push to your registry |
| `REGISTRY_PASSWORD` | yes | That account's password or robot token |
| `AWS_ACCESS_KEY_ID` | if 8+ characters | Object-store access key |
| `AWS_SECRET_ACCESS_KEY` | yes | Object-store secret key |

GitLab refuses to mask a value under 8 characters, which short account names
often are. Leave **Protect variable** off unless your default branch is
protected.

## 4. Push and watch the two pipelines

Push a branch and open a merge request.

- **Merge request pipeline**: `mirror:container-list-rke2` writes the reference
  list, `mirror:container-mirror-skopeo` runs in dry run. It inspects every
  reference and prints its digest. Nothing is written to the registry. The
  export job does not run.
- **Default-branch pipeline**, after you merge: the same list job, then the
  mirror actually copies, then `mirror:container-export-skopeo` pulls the
  mirrored images into one OCI layout, drops the blobs named by `have-key`, and
  uploads `transfer-<stamp>.tar` to the object store.

Read these two lines in the job logs:

```text
== 2 reference(s) -> .ci-artifacts/mirror/container-mirror-skopeo/digests.txt
checksums back: 0 blob(s) already on the receiving side, left out
```

## 5. Verify one image landed

```bash
skopeo inspect --format '{{.Digest}}' \
  docker://registry.example.com/mirror/library/alpine:3.21
```

The digest must equal the one on the mirror job's line for that reference.

## 6. Receive it on the far side

The importer, the persistent OCI store and the NiFi send/receive flows are in
[`../quay-layer-transfer/`](../quay-layer-transfer/). The tar this pipeline
uploads is exactly what its receive flow delivers: unpack it into
`data/high-store/incoming/transfer-<stamp>/` and run `scripts/import.sh`.

Import writes `data/low-export/have/blobs.txt`, the sorted list of blob digests
the far store now holds. Upload that file to the object key you named in
`have-key`, and the next export leaves those blobs out.

## 7. Keep the references current

Copy `examples/consumer/renovate.json` into the consumer project. It has two
custom managers: one bumps the tag and digest of every line of `images.txt`,
one bumps the `ref:` of the include.

A bump merge request changes one line:

```diff
-docker.io/library/alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d
+docker.io/library/alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce
```

The merge request pipeline dry-runs it. Merge, and the default branch mirrors
and exports it.

## Inputs

Of `pipelines/container-mirror.yml`.

| Input | Default | What it is |
|---|---|---|
| `instance` | **required** | Workload identifier; every job name starts with it. |
| `changes-paths` | `**/*` | Paths that make this pipeline eligible on a merge request or a branch push. Schedules ignore it. A narrowed list must still name `.gitlab-ci.yml`, or a bump of the shared-CI ref runs nothing. |
| `config-file` | (empty) | RKE2 release manifest read by the list job. Empty resolves no release and the mirror works from extra-list-files alone. |
| `extra-list-files` | `images.txt` | Space-separated relative paths of additional reference lists in the consumer checkout, mirrored alongside the resolved release list. |
| `registry` | (empty) | Destination registry host. Required unless vault-addr is set, in which case an empty value is read from Vault. |
| `repository-prefix` | **required** | Path under the registry every reference is placed in. |
| `arch` | `linux-amd64` | Platform selected from a multi-platform reference. |
| `s3-endpoint` | **required** | S3 API endpoint the transfer archive is uploaded to. |
| `s3-bucket` | **required** | Bucket holding the transfer prefix and the checksum list. |
| `s3-prefix` | **required** | Key prefix the archive is uploaded under. |
| `have-key` | **required** | Object key listing the blobs the receiving side already holds. |
| `vault-addr` | (empty) | Vault API base URL, also the JWT audience. Empty, the default, keeps both jobs on the CI/CD variables of step 3. |
| `vault-role` | (empty) | Vault JWT role the CI identity logs in with. Vault mode only. |
| `vault-kv-path` | (empty) | KV v2 path holding the registry credential. Vault mode only. |
| `vault-field-registry` | `registry` | Field holding the registry host. |
| `vault-field-user` | `username` | Field holding the robot account name. |
| `vault-field-token` | `token` | Field holding the robot token. |
| `vault-s3-kv-path` | (empty) | KV v2 path holding the S3 credential. Vault mode only. |
| `vault-s3-field-access-key` | `access_key` | Field holding the S3 access key id. |
| `vault-s3-field-secret-key` | `secret_key` | Field holding the S3 secret key. |
| `ca-bundle-url` | (empty) | Optional PEM bundle trusted before any registry call. |
| `egress-proxy` | (empty) | HTTP CONNECT proxy for outbound traffic. |
| `no-proxy` | (empty) | NO_PROXY value applied alongside egress-proxy. |
| `execution-image` | `docker.io/library/alpine@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d` | Approved execution image pinned by digest. |
| `runner-tags` | (none) | Approved runner selection. |

## Requirements

- GitLab 17.0 or later, for `spec:inputs` with typed inputs and regex
  validation.
- A runner whose executor gives each job its own container, `docker` or
  `kubernetes`. A shell executor works if `apk` is available and jobs do not
  share a filesystem.
- Outbound access from the runner to: every registry named in `images.txt`
  (`docker.io` and `*.docker.io` for the example file), your destination
  registry, your S3 endpoint, and `github.com` only if you set `config-file`.
- Disk on the runner for the whole image set uncompressed, in the export job.

# shellcheck shell=bash
# Shared RPM download + createrepo helpers. Source only.
# Package lists: $OTK_CATALOG/rpm/<id>.txt → $RPM_ARTIFACT_ROOT/<id>/{*.rpm,repodata/}
# Upstream repos: $OTK_CATALOG/rpm/repos.yml → temporary dnf .repo + offline snippets.

PYTHON="${OTK_PYTHON:-${PYTHON:-python3}}"
RPM_ARTIFACT_ROOT="${RPM_ARTIFACT_ROOT:-${STAGING:-.}/artifacts/rpm}"

rpm_use_docker() {
  local mode="${OTK_RPM_VIA_DOCKER:-auto}"
  case "$mode" in
    always|1|true|yes) return 0 ;;
    never|0|false|no) return 1 ;;
    auto|*)
      if command -v dnf >/dev/null 2>&1 || command -v yumdownloader >/dev/null 2>&1; then
        return 1
      fi
      command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1
      ;;
  esac
}

rpm_container_runtime() {
  if command -v docker >/dev/null 2>&1; then
    echo docker
  elif command -v podman >/dev/null 2>&1; then
    echo podman
  else
    return 1
  fi
}

# Populates OTK_RPM_DNF_DIR (absolute) with temporary low-side .repo files.
rpm_stage_repo_files() {
  local dnf_dir="$OTK_WORK/rpm-dnf-repos"
  local offline_dir="$RPM_ARTIFACT_ROOT/client-repos"
  local repos_yml="$OTK_CATALOG/rpm/repos.yml"
  rm -rf "$dnf_dir"
  mkdir -p "$dnf_dir" "$offline_dir" "$RPM_ARTIFACT_ROOT/keys"
  if [[ -f "$repos_yml" ]]; then
    "$PYTHON" "$SCRIPT_DIR/lib/write_rpm_repo_files.py" "$repos_yml" \
      --dnf-dir "$dnf_dir" \
      --offline-dir "$offline_dir" \
      --base-url "${OTK_PKG_BASE_URL:-https://pkg.example.invalid}" >&2
    # Fetch GPG keys listed in repos.yml (best-effort; network on low side only)
    while IFS= read -r keyurl; do
      [[ -z "$keyurl" || "$keyurl" != http* ]] && continue
      local kn
      kn="$(echo "$keyurl" | sed 's|.*/||;s/[^A-Za-z0-9._-]/_/g')"
      [[ -z "$kn" || "$kn" == "_" ]] && kn="gpgkey.asc"
      log "rpm: fetch gpg key $keyurl"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$keyurl" -o "$RPM_ARTIFACT_ROOT/keys/$kn" || \
          log "WARN: gpg key fetch failed: $keyurl"
      fi
    done < <(grep -E '^\s*gpgkey:' "$repos_yml" | sed 's/.*gpgkey:[[:space:]]*//;s/[\"'\'']//g' || true)
  fi
  local list id
  shopt -s nullglob
  for list in "$OTK_CATALOG"/rpm/*.txt; do
    id="$(basename "$list" .txt)"
    file_has_entries "$list" || continue
    if [[ ! -f "$offline_dir/offline-${id}.repo.example" ]]; then
      cat >"$offline_dir/offline-${id}.repo.example" <<EOF
# High-side client snippet — /etc/yum.repos.d/offline-${id}.repo
[offline-${id}]
name=Offline ${id}
baseurl=${OTK_PKG_BASE_URL:-https://pkg.example.invalid}/rpm/${id}/
enabled=1
gpgcheck=0
module_hotfixes=1
EOF
    fi
  done
  shopt -u nullglob
  OTK_RPM_DNF_DIR="$(cd "$dnf_dir" && pwd)"
  export OTK_RPM_DNF_DIR
  local nrepos
  nrepos="$(find "$OTK_RPM_DNF_DIR" -name '*.repo' 2>/dev/null | wc -l | tr -d ' ')"
  log "rpm: staged $nrepos dnf repo file(s) under $OTK_RPM_DNF_DIR"
}

rpm_createrepo_host() {
  local dest="$1"
  if command -v createrepo_c >/dev/null 2>&1; then
    createrepo_c "$dest"
  elif command -v createrepo >/dev/null 2>&1; then
    createrepo "$dest"
  else
    return 1
  fi
}

rpm_download_host() {
  local list="$1" dest="$2" dnf_dir="$3"
  local -a pkgs=()
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    pkgs+=("$line")
  done <"$list"
  [[ ${#pkgs[@]} -gt 0 ]] || return 0
  if command -v dnf >/dev/null 2>&1; then
    local rd_args=()
    if [[ -d "$dnf_dir" ]] && compgen -G "$dnf_dir/*.repo" >/dev/null 2>&1; then
      rd_args=(--setopt="reposdir=$dnf_dir:/etc/yum.repos.d")
    fi
    dnf download --resolve --alldeps --destdir "$dest" \
      "${rd_args[@]}" \
      "${pkgs[@]}" || return 1
  else
    yumdownloader --resolve --destdir "$dest" "${pkgs[@]}" || return 1
  fi
  rpm_createrepo_host "$dest" || log "WARN: createrepo(_c) missing — repo metadata not generated"
}

rpm_download_docker() {
  local list="$1" dest="$2" dnf_dir="$3" id="$4"
  local runtime image platform
  runtime="$(rpm_container_runtime)" || die "rpm: docker/podman required for containerized download"
  image="${OTK_RPM_DOCKER_IMAGE:-almalinux:9}"
  platform="${OTK_RPM_DOCKER_PLATFORM:-linux/amd64}"
  log "rpm: docker download id=$id image=$image platform=$platform"

  "$runtime" pull --platform "$platform" "$image" >/dev/null 2>&1 || \
    "$runtime" pull "$image" >/dev/null 2>&1 || true

  # Colima/Docker Desktop cannot bind-mount /var/folders or some /tmp paths.
  # Stage under $HOME (or OTK_WORK when that is already home-visible), then copy.
  local docker_root="$OTK_WORK"
  case "$docker_root" in
    /var/folders/*|/private/var/folders/*|/tmp/*)
      docker_root="${HOME}/.cache/otk-rpm-docker"
      log "rpm: OTK_WORK is not bind-mountable; staging under $docker_root"
      ;;
  esac
  local docker_dest="$docker_root/rpm-docker-out/$id"
  rm -rf "$docker_dest"
  mkdir -p "$docker_dest" "$dest"

  local list_base dest_base dnf_base
  list_base="$(cd "$(dirname "$list")" && pwd)"
  dest_base="$(cd "$docker_dest" && pwd)"
  dnf_base=""
  if [[ -d "$dnf_dir" ]]; then
    dnf_base="$(cd "$dnf_dir" && pwd)"
  fi

  local -a run_args=(run --rm --platform "$platform"
    -v "$list_base:/catalog/rpm:ro"
    -v "$dest_base:/out"
  )
  if [[ -n "$dnf_base" && -d "$dnf_base" ]]; then
    run_args+=(-v "$dnf_base:/otk-repos:ro")
    log "rpm: mounting dnf repos from $dnf_base"
  else
    log "WARN: rpm: no dnf repo dir to mount (third-party lists may fail)"
  fi
  run_args+=(-e "OTK_LIST=/catalog/rpm/$(basename "$list")" -e "OTK_ID=$id")
  local force_arch=""
  case "$platform" in
    *amd64*|*x86_64*) force_arch=x86_64 ;;
    *arm64*|*aarch64*) force_arch=aarch64 ;;
  esac
  run_args+=(-e "OTK_FORCE_ARCH=$force_arch")
  run_args+=("$image")
  "$runtime" "${run_args[@]}" \
    bash -ec '
      set -euo pipefail
      dnf -y install dnf-plugins-core createrepo_c ca-certificates >/dev/null
      if [[ -d /otk-repos ]]; then
        cp -a /otk-repos/. /etc/yum.repos.d/ || true
      fi
      dnf -y makecache >/dev/null || true
      mapfile -t pkgs < <(grep -vE "^\s*(#|$)" "$OTK_LIST")
      echo "packages: ${pkgs[*]}"
      arch_args=()
      if [[ -n "${OTK_FORCE_ARCH:-}" ]]; then
        arch_args=(--forcearch="$OTK_FORCE_ARCH")
      fi
      dnf download --resolve --alldeps --destdir /out "${arch_args[@]}" "${pkgs[@]}"
      createrepo_c /out
      echo "rpm-ok id=$OTK_ID count=$(ls -1 /out/*.rpm 2>/dev/null | wc -l)"
    ' || die "rpm: containerized download failed for $id"
  cp -a "$docker_dest"/. "$dest"/
}

build_rpm() {
  if declare -F want_component >/dev/null 2>&1; then
    want_component rpm || return 0
  fi
  local any=0
  local list id dest
  rpm_stage_repo_files
  local dnf_dir="${OTK_RPM_DNF_DIR:-}"

  local use_docker=0
  if rpm_use_docker; then
    use_docker=1
    log "rpm: using containerized dnf+createrepo_c (OTK_RPM_VIA_DOCKER=${OTK_RPM_VIA_DOCKER:-auto})"
  elif ! command -v dnf >/dev/null 2>&1 && ! command -v yumdownloader >/dev/null 2>&1; then
    log "WARN: rpm: no host dnf and no docker — skip all RPM lists (set OTK_RPM_VIA_DOCKER=always with docker)"
    return 0
  fi

  shopt -s nullglob
  for list in "$OTK_CATALOG"/rpm/*.txt; do
    id="$(basename "$list" .txt)"
    if ! file_has_entries "$list"; then
      log "rpm: $id empty — skip"
      continue
    fi
    any=1
    dest="$RPM_ARTIFACT_ROOT/$id"
    mkdir -p "$dest"
    log "rpm: download --resolve for $id"
    if [[ $use_docker -eq 1 ]]; then
      rpm_download_docker "$list" "$dest" "$dnf_dir" "$id"
    else
      rpm_download_host "$list" "$dest" "$dnf_dir" || die "dnf download failed for $id"
    fi
    local n
    n="$(find "$dest" -maxdepth 1 -name '*.rpm' -type f | wc -l | tr -d ' ')"
    [[ "$n" -gt 0 ]] || die "rpm: no RPMs landed in $dest for $id"
    [[ -d "$dest/repodata" ]] || die "rpm: missing repodata/ for $id (createrepo failed)"
    log "rpm: $id → $n rpm(s) + repodata"
    if [[ -f "$RPM_ARTIFACT_ROOT/client-repos/offline-${id}.repo.example" ]]; then
      cp -a "$RPM_ARTIFACT_ROOT/client-repos/offline-${id}.repo.example" \
        "$dest/offline.repo.example"
    fi
    cat >"$dest/INSTALL.txt" <<EOF
Offline RPM repo: $id

High-side (HTTP after ingest into Pulp or serve root):
  baseurl=${OTK_PKG_BASE_URL:-https://pkg.example.invalid}/rpm/${id}/

Or file://:
  baseurl=file:///srv/offline/rpm/${id}/

  dnf install -y --repofrompath=offline-${id},${OTK_PKG_BASE_URL:-https://pkg.example.invalid}/rpm/${id}/ \\
    --setopt=offline-${id}.gpgcheck=0 PACKAGE

See offline.repo.example for a drop-in /etc/yum.repos.d/ snippet.
EOF
  done
  shopt -u nullglob
  [[ $any -eq 1 ]] || log "rpm: no package lists with entries"
}

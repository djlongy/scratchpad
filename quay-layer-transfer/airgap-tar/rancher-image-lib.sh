#!/bin/bash
#
# Derived from the air-gap image scripts published with Rancher releases
# (rancher/rancher, Apache License 2.0). Substantially modified: digest-tag
# pinning, image-label version resolution, a lock file, platform pinning and an
# upstream-drift check were added; the original argument handling and usage text
# are retained so the tools remain drop-in compatible.
#
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Shared helpers for rancher-save-images.sh / rancher-load-images.sh.
# Docker CLI + bash only (no crane, skopeo, python, curl).
#
# Bitnami community images on docker.io no longer publish version tags — only
# `latest` plus Cosign/provenance tags named `sha256-<digest>`. Those named
# tags are NOT the application image (their digest differs from `latest`).
# A basic Docker Registry also rewrites the manifest on push, so the Hub
# digest cannot be used as a pull ref on the destination.
#
# Pin with a *tag name* equal to the Hub index digest (`sha256-<hex>`), and
# keep the app version from image labels as a human alias. Watch `latest`
# (or the original list tag) for digest movement.

_ri_err() { echo "ERROR: $*" >&2; }

# docker.io / index.docker.io / registry-1.docker.io are the same Hub.
ri_strip_docker_io() {
    local i="$1"
    i="${i#docker.io/}"
    i="${i#index.docker.io/}"
    i="${i#registry-1.docker.io/}"
    printf '%s\n' "$i"
}

# Path without registry host, tag, or digest. "bitnami/redis" or "rancher/rancher".
# Do not name locals `path` — zsh ties `path` to PATH.
ri_image_path() {
    local i
    i="$(ri_strip_docker_io "$1")"
    i="${i%%@*}"
    i="${i%%:*}"
    case "${i%%/*}" in
        *.*|*:*|localhost) i="${i#*/}" ;;
    esac
    printf '%s\n' "$i"
}

ri_image_tag() {
    local i="$1"
    case "$i" in
        *@sha256:*) printf '\n'; return ;;
        *:*)
            local tag="${i##*:}"
            case "$tag" in
                */*) printf 'latest\n' ;;
                *) printf '%s\n' "$tag" ;;
            esac
            ;;
        *) printf 'latest\n' ;;
    esac
}

ri_image_digest() {
    local i="$1"
    case "$i" in
        *@sha256:*) printf '%s\n' "${i##*@}" ;;
        *) printf '\n' ;;
    esac
}

ri_is_sha_named_tag() {
    [[ "$1" =~ ^sha256-[0-9a-f]{64}$ ]]
}

ri_is_attestation_ref() {
    local tag
    tag="$(ri_image_tag "$1")"
    case "$tag" in
        *.sig|*.att|*.metadata|*metadata) return 0 ;;
    esac
    return 1
}

# Hub tag `sha256-<hex>` is provenance for digest sha256:<hex>, not the image.
# Pull by digest instead.
ri_pull_ref() {
    local ref="$1" img_path tag digest
    digest="$(ri_image_digest "$ref")"
    if [ -n "$digest" ]; then
        printf '%s@%s\n' "$(ri_image_path "$ref")" "$digest"
        return
    fi
    img_path="$(ri_image_path "$ref")"
    tag="$(ri_image_tag "$ref")"
    if ri_is_sha_named_tag "$tag"; then
        printf '%s@sha256:%s\n' "$img_path" "${tag#sha256-}"
        return
    fi
    printf '%s:%s\n' "$img_path" "$tag"
}

ri_digest_tag_name() {
    local d="$1"
    d="${d#sha256:}"
    printf 'sha256-%s\n' "$d"
}

# Floating tag we re-query to see if Hub moved. Digest pins track `latest`.
ri_track_ref() {
    local ref="$1" img_path tag digest
    img_path="$(ri_image_path "$ref")"
    digest="$(ri_image_digest "$ref")"
    tag="$(ri_image_tag "$ref")"
    if [ -n "$digest" ] || ri_is_sha_named_tag "$tag"; then
        printf '%s:latest\n' "$img_path"
        return
    fi
    printf '%s:%s\n' "$img_path" "$tag"
}

ri_inspect_app_version() {
    local img="$1" ver
    ver="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$img" 2>/dev/null || true)"
    if [ -z "$ver" ] || [ "$ver" = "<no value>" ]; then
        ver="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$img" 2>/dev/null | sed -n 's/^APP_VERSION=//p' | head -1)"
    fi
    if [ -z "$ver" ]; then
        ver="$(docker inspect --format '{{index .Config.Labels "app.kubernetes.io/version"}}' "$img" 2>/dev/null || true)"
        [ "$ver" = "<no value>" ] && ver=""
    fi
    printf '%s\n' "$ver"
}

# Parse `Digest: sha256:...` from docker pull / buildx imagetools stdout.
# First Digest: line is the Hub *index* digest for a multi-arch tag.
ri_parse_pull_digest() {
    awk '/^Digest:/{print $2; exit}' | tr -d '\r'
}

ri_docker_pull() {
    local ref="$1" platform="$2"
    local -a args=(pull)
    if [ -n "$platform" ]; then
        args+=(--platform "$platform")
    fi
    args+=("$ref")
    docker "${args[@]}"
}

ri_save_supports_platform() {
    docker save --help 2>&1 | grep -q -- '--platform'
}

# Current Hub index digest. Prefers `docker buildx imagetools inspect` (manifest
# only, no layer pull). Falls back to `docker pull`, which prints Digest: even
# when layers are already present.
ri_upstream_digest() {
    local ref="$1" img_path tag digest name out
    img_path="$(ri_image_path "$ref")"
    digest="$(ri_image_digest "$ref")"
    tag="$(ri_image_tag "$ref")"
    if [ -n "$digest" ]; then
        printf '%s\n' "$digest"
        return
    fi
    if ri_is_sha_named_tag "$tag"; then
        printf 'sha256:%s\n' "${tag#sha256-}"
        return
    fi
    name="${img_path}:${tag}"
    out="$(docker buildx imagetools inspect "docker.io/${name}" 2>/dev/null || true)"
    if [ -n "$out" ]; then
        printf '%s\n' "$out" | ri_parse_pull_digest
        return
    fi
    out="$(docker pull ${RI_PLATFORM:+--platform "$RI_PLATFORM"} "$name" 2>&1)" || return 1
    printf '%s\n' "$out" | ri_parse_pull_digest
}

ri_write_lock_header() {
    local lock="$1"
    {
        echo "# rancher-images.lock"
        echo "# pin = source_digest_tag (Hub index digest as a tag name)"
        echo "# track_ref = floating tag re-queried by --check-upstream"
        echo "# generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo
    } > "$lock"
}

ri_write_lock_record() {
    local lock="$1"
    {
        echo "list_ref=$2"
        echo "pull_ref=$3"
        echo "track_ref=$4"
        echo "source_digest=$5"
        echo "source_digest_tag=$6"
        echo "app_version=$7"
        echo "save_name=$8"
        echo "local_tags=$9"
        echo "platform=${10}"
        echo
    } >> "$lock"
}

ri_check_upstream() {
    local lock="$1"
    if [ ! -f "$lock" ]; then
        _ri_err "lock file not found: ${lock}"
        return 1
    fi

    local list_ref="" pull_ref="" track_ref="" source_digest="" source_digest_tag=""
    local app_version="" save_name="" local_tags="" platform=""
    local match=0 update=0 err=0
    local current st short_p short_c

    _ri_flush_record() {
        [ -n "$source_digest" ] || return 0
        current="$(ri_upstream_digest "$track_ref")"
        if [ -z "$current" ]; then
            st="ERROR"
            err=$((err + 1))
        elif [ "$current" = "$source_digest" ]; then
            st="MATCH"
            match=$((match + 1))
        else
            st="UPDATE"
            update=$((update + 1))
        fi
        short_p="${source_digest#sha256:}"
        short_p="${short_p:0:12}…"
        short_c="${current:-unresolved}"
        short_c="${short_c#sha256:}"
        short_c="${short_c:0:12}…"
        printf '%-8s %-10s %-36s %s  →  %s\n' "$st" "${app_version:-?}" "$track_ref" "$short_p" "$short_c"
        if [ "$st" = "UPDATE" ]; then
            echo "         pinned tag : ${source_digest_tag}"
            echo "         pinned     : ${source_digest}"
            echo "         upstream   : ${current}"
            echo "         Hub moved. Re-run rancher-save-images.sh to refresh the tar."
        fi
        if [ "$st" = "ERROR" ]; then
            echo "         could not read digest for ${track_ref}"
        fi
        list_ref=""; pull_ref=""; track_ref=""; source_digest=""; source_digest_tag=""
        app_version=""; save_name=""; local_tags=""; platform=""
    }

    echo "STATUS   APP        TRACK                                PINNED  →  UPSTREAM"
    echo "----------------------------------------------------------------------------------------------------"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*)
                _ri_flush_record
                ;;
            list_ref=*) list_ref="${line#list_ref=}" ;;
            pull_ref=*) pull_ref="${line#pull_ref=}" ;;
            track_ref=*) track_ref="${line#track_ref=}" ;;
            source_digest=*) source_digest="${line#source_digest=}" ;;
            source_digest_tag=*) source_digest_tag="${line#source_digest_tag=}" ;;
            app_version=*) app_version="${line#app_version=}" ;;
            save_name=*) save_name="${line#save_name=}" ;;
            local_tags=*) local_tags="${line#local_tags=}" ;;
            platform=*) platform="${line#platform=}" ;;
        esac
    done < "$lock"
    _ri_flush_record
    echo "----------------------------------------------------------------------------------------------------"
    echo "${match} match, ${update} update, ${err} error"
    [ "$update" -eq 0 ] && [ "$err" -eq 0 ]
}

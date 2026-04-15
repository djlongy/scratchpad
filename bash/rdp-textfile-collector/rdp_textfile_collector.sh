#!/usr/bin/env bash
# rdp_textfile_collector.sh
#
# node_exporter textfile collector for RDP (TCP/3389) connection health.
# Emits per-peer TCP metrics derived from `ss -tin` (kernel tcp_info) so you
# can alert on RTT, retransmits, lost/reordered packets, and timeouts.
#
# Works on both sides:
#   - RDP server host (xrdp / Windows-in-Linux-bridge): sport = :3389
#   - RDP client/jump host:                              dport = :3389
#
# Usage (cron or systemd timer, every 15s):
#   TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector \
#     ./rdp_textfile_collector.sh
#
# node_exporter must be started with:
#   --collector.textfile.directory=/var/lib/node_exporter/textfile_collector
#
# Writes atomically to $TEXTFILE_DIR/rdp.prom.

set -eo pipefail

PORT="${RDP_PORT:-3389}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
OUT="${TEXTFILE_DIR}/rdp.prom"
TMP="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

if ! command -v ss >/dev/null 2>&1; then
  echo "ss not found (install iproute2)" >&2
  exit 1
fi

# ss -tinH:
#   -t tcp, -i tcp_info, -n numeric, -H no header
# Each established connection produces two lines:
#   line 1: State Recv-Q Send-Q Local:Port Peer:Port
#   line 2: indented tcp_info fields (cubic wscale:... rto:... rtt:.../... ...)
#
# Relevant fields in the tcp_info line we care about:
#   rto:<ms>                         retransmission timeout
#   rtt:<ms>/<rttvar_ms>             smoothed RTT and variance
#   minrtt:<ms>
#   retrans:<cur>/<total>            retransmits (current queue / lifetime)
#   lost:<n>                         packets the stack thinks are lost
#   reordering:<n>                   reorder metric (default 3, rises on reorder)
#   reord_seen:<n>                   kernel saw actual reorder events (5.x+)
#   unacked:<n>
#   bytes_sent:<n>  bytes_retrans:<n>
#   data_segs_out:<n>                segments sent (for retrans ratio)

declare -A C_CONNS C_RTT_SUM C_RTTVAR_SUM C_MINRTT_MIN
declare -A C_RETRANS_CUR C_RETRANS_TOT C_LOST C_REORD C_REORD_SEEN
declare -A C_BYTES_SENT C_BYTES_RETRANS C_SEGS_OUT C_RTO_MAX C_UNACKED

emit_metric() {
  echo "$1" >> "$TMP"
}

# Parse ss output. Join every pair of lines into a single record.
# Use process substitution so the while-loop runs in the current shell and
# associative-array updates survive.
while IFS= read -r line; do
      hdr="${line%% || *}"
      info="${line##* || }"

      # Columns in the header line (no State column when filtered by state):
      #   Recv-Q Send-Q Local:Port Peer:Port [Process]
      # shellcheck disable=SC2086
      set -- $hdr
      local_addr="$3"
      peer_addr="$4"

      # Direction: are we the server (sport=3389) or the client (dport=3389)?
      local_port="${local_addr##*:}"
      if [[ "$local_port" == "$PORT" ]]; then
        direction="server"
        peer="${peer_addr%:*}"
      else
        direction="client"
        peer="${peer_addr%:*}"
      fi
      # Strip IPv6 brackets and IPv4-mapped-IPv6 prefix for cleaner labels.
      peer="${peer#[}"
      peer="${peer%]}"
      peer="${peer#::ffff:}"

      # Extract fields from the tcp_info line.
      rtt=""; rttvar=""; minrtt=""
      retrans_cur=0; retrans_tot=0
      lost=0; reord=0; reord_seen=0
      bytes_sent=0; bytes_retrans=0; segs_out=0
      rto=0; unacked=0

      for tok in $info; do
        case "$tok" in
          rto:*)            rto="${tok#rto:}" ;;
          rtt:*)            rttpair="${tok#rtt:}"; rtt="${rttpair%/*}"; rttvar="${rttpair#*/}" ;;
          minrtt:*)         minrtt="${tok#minrtt:}" ;;
          retrans:*)        rpair="${tok#retrans:}"; retrans_cur="${rpair%/*}"; retrans_tot="${rpair#*/}" ;;
          lost:*)           lost="${tok#lost:}" ;;
          reordering:*)     reord="${tok#reordering:}" ;;
          reord_seen:*)     reord_seen="${tok#reord_seen:}" ;;
          bytes_sent:*)     bytes_sent="${tok#bytes_sent:}" ;;
          bytes_retrans:*)  bytes_retrans="${tok#bytes_retrans:}" ;;
          data_segs_out:*)  segs_out="${tok#data_segs_out:}" ;;
          unacked:*)        unacked="${tok#unacked:}" ;;
        esac
      done

      key="${direction}|${peer}"
      C_CONNS[$key]=$(( ${C_CONNS[$key]:-0} + 1 ))
      # Aggregate as float via awk to avoid bash int-only math.
      read -r rtt_sum rttvar_sum minrtt_min rto_max <<<"$(awk -v a="${C_RTT_SUM[$key]:-0}" -v b="${rtt:-0}" \
                                                               -v c="${C_RTTVAR_SUM[$key]:-0}" -v d="${rttvar:-0}" \
                                                               -v e="${C_MINRTT_MIN[$key]:-}" -v f="${minrtt:-0}" \
                                                               -v g="${C_RTO_MAX[$key]:-0}" -v h="${rto:-0}" \
        'BEGIN{
           printf "%.6f %.6f %.6f %.6f",
             a+b, c+d,
             (e==""?f:(f<e?f:e)),
             (h>g?h:g);
         }')"
      C_RTT_SUM[$key]=$rtt_sum
      C_RTTVAR_SUM[$key]=$rttvar_sum
      C_MINRTT_MIN[$key]=$minrtt_min
      C_RTO_MAX[$key]=$rto_max

      C_RETRANS_CUR[$key]=$(( ${C_RETRANS_CUR[$key]:-0} + retrans_cur ))
      C_RETRANS_TOT[$key]=$(( ${C_RETRANS_TOT[$key]:-0} + retrans_tot ))
      C_LOST[$key]=$(( ${C_LOST[$key]:-0} + lost ))
      C_REORD[$key]=$(( ${C_REORD[$key]:-0} + reord ))
      C_REORD_SEEN[$key]=$(( ${C_REORD_SEEN[$key]:-0} + reord_seen ))
      C_BYTES_SENT[$key]=$(( ${C_BYTES_SENT[$key]:-0} + bytes_sent ))
      C_BYTES_RETRANS[$key]=$(( ${C_BYTES_RETRANS[$key]:-0} + bytes_retrans ))
      C_SEGS_OUT[$key]=$(( ${C_SEGS_OUT[$key]:-0} + segs_out ))
      C_UNACKED[$key]=$(( ${C_UNACKED[$key]:-0} + unacked ))
done < <(ss -tinH state established "( sport = :${PORT} or dport = :${PORT} )" 2>/dev/null \
           | awk 'NR%2{ hdr=$0; next } { print hdr " || " $0 }')

# Header / HELP+TYPE blocks.
{
  cat <<'EOF'
# HELP rdp_tcp_connections Current established RDP TCP connections.
# TYPE rdp_tcp_connections gauge
# HELP rdp_tcp_rtt_seconds Mean smoothed RTT across connections to this peer.
# TYPE rdp_tcp_rtt_seconds gauge
# HELP rdp_tcp_rttvar_seconds Mean RTT variance across connections to this peer.
# TYPE rdp_tcp_rttvar_seconds gauge
# HELP rdp_tcp_min_rtt_seconds Minimum observed RTT across connections to this peer.
# TYPE rdp_tcp_min_rtt_seconds gauge
# HELP rdp_tcp_rto_seconds Max current retransmission timeout across connections.
# TYPE rdp_tcp_rto_seconds gauge
# HELP rdp_tcp_retrans_in_flight Packets currently awaiting retransmission.
# TYPE rdp_tcp_retrans_in_flight gauge
# HELP rdp_tcp_retrans_total Lifetime retransmits summed across current connections.
# TYPE rdp_tcp_retrans_total gauge
# HELP rdp_tcp_lost_packets Packets the kernel considers lost right now.
# TYPE rdp_tcp_lost_packets gauge
# HELP rdp_tcp_reordering Kernel reordering metric (defaults to 3, grows on reorder).
# TYPE rdp_tcp_reordering gauge
# HELP rdp_tcp_reord_seen_total Actual reorder events observed by the kernel (5.x+).
# TYPE rdp_tcp_reord_seen_total gauge
# HELP rdp_tcp_bytes_sent Bytes sent on current connections.
# TYPE rdp_tcp_bytes_sent gauge
# HELP rdp_tcp_bytes_retrans Bytes retransmitted on current connections.
# TYPE rdp_tcp_bytes_retrans gauge
# HELP rdp_tcp_data_segs_out Data segments sent on current connections.
# TYPE rdp_tcp_data_segs_out gauge
# HELP rdp_tcp_unacked_segments Unacknowledged segments in flight.
# TYPE rdp_tcp_unacked_segments gauge
EOF
} >> "$TMP"

for key in "${!C_CONNS[@]}"; do
  direction="${key%%|*}"
  peer="${key##*|}"
  labels="direction=\"${direction}\",peer=\"${peer}\",port=\"${PORT}\""

  conns=${C_CONNS[$key]}
  # Convert ms-based fields to seconds and mean-aggregate RTT / rttvar.
  read -r mean_rtt mean_rttvar min_rtt rto_s <<<"$(awk -v n="$conns" \
      -v rs="${C_RTT_SUM[$key]:-0}" -v vs="${C_RTTVAR_SUM[$key]:-0}" \
      -v mn="${C_MINRTT_MIN[$key]:-0}" -v rto="${C_RTO_MAX[$key]:-0}" \
    'BEGIN{
       mr  = (n>0 ? (rs/n)/1000.0 : 0);
       mv  = (n>0 ? (vs/n)/1000.0 : 0);
       mnr = mn/1000.0;
       rs2 = rto/1000.0;
       printf "%.6f %.6f %.6f %.6f", mr, mv, mnr, rs2;
     }')"

  emit_metric "rdp_tcp_connections{${labels}} ${conns}"
  emit_metric "rdp_tcp_rtt_seconds{${labels}} ${mean_rtt}"
  emit_metric "rdp_tcp_rttvar_seconds{${labels}} ${mean_rttvar}"
  emit_metric "rdp_tcp_min_rtt_seconds{${labels}} ${min_rtt}"
  emit_metric "rdp_tcp_rto_seconds{${labels}} ${rto_s}"
  emit_metric "rdp_tcp_retrans_in_flight{${labels}} ${C_RETRANS_CUR[$key]:-0}"
  emit_metric "rdp_tcp_retrans_total{${labels}} ${C_RETRANS_TOT[$key]:-0}"
  emit_metric "rdp_tcp_lost_packets{${labels}} ${C_LOST[$key]:-0}"
  emit_metric "rdp_tcp_reordering{${labels}} ${C_REORD[$key]:-0}"
  emit_metric "rdp_tcp_reord_seen_total{${labels}} ${C_REORD_SEEN[$key]:-0}"
  emit_metric "rdp_tcp_bytes_sent{${labels}} ${C_BYTES_SENT[$key]:-0}"
  emit_metric "rdp_tcp_bytes_retrans{${labels}} ${C_BYTES_RETRANS[$key]:-0}"
  emit_metric "rdp_tcp_data_segs_out{${labels}} ${C_SEGS_OUT[$key]:-0}"
  emit_metric "rdp_tcp_unacked_segments{${labels}} ${C_UNACKED[$key]:-0}"
done

# If there are zero connections, still emit a zeroed gauge so alerts can fire
# on absence rather than staleness.
if [[ ${#C_CONNS[@]} -eq 0 ]]; then
  emit_metric "rdp_tcp_connections{direction=\"any\",peer=\"none\",port=\"${PORT}\"} 0"
fi

# Also capture host-wide TCP retransmit / reorder counters from /proc/net/netstat
# as a secondary signal — these are global, not port-scoped, but useful for
# correlating against the per-connection numbers above.
if [[ -r /proc/net/netstat ]]; then
  awk '
    /^TcpExt:/ {
      if (hdr=="") { for(i=2;i<=NF;i++) h[i]=$i; hdr=1; next }
      for(i=2;i<=NF;i++) v[h[i]]=$i
    }
    END {
      printf "# HELP rdp_host_tcp_retrans_segs Host-wide TCP RetransSegs counter.\n"
      printf "# TYPE rdp_host_tcp_retrans_segs counter\n"
      printf "rdp_host_tcp_retrans_segs %s\n", v["RetransSegs"]+0
      printf "# HELP rdp_host_tcp_lost_retransmit Host-wide TCPLostRetransmit counter.\n"
      printf "# TYPE rdp_host_tcp_lost_retransmit counter\n"
      printf "rdp_host_tcp_lost_retransmit %s\n", v["TCPLostRetransmit"]+0
      printf "# HELP rdp_host_tcp_timeouts Host-wide TCPTimeouts counter.\n"
      printf "# TYPE rdp_host_tcp_timeouts counter\n"
      printf "rdp_host_tcp_timeouts %s\n", v["TCPTimeouts"]+0
      printf "# HELP rdp_host_tcp_reordering Host-wide TCPSACKReorder + TCPRenoReorder + TCPTSReorder + TCPFACKReorder.\n"
      printf "# TYPE rdp_host_tcp_reordering counter\n"
      printf "rdp_host_tcp_reordering %s\n", (v["TCPSACKReorder"]+v["TCPRenoReorder"]+v["TCPTSReorder"]+v["TCPFACKReorder"])+0
    }
  ' /proc/net/netstat >> "$TMP"
fi

chmod 0644 "$TMP"
mv -f "$TMP" "$OUT"
trap - EXIT

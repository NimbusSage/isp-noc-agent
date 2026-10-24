# Variance — Full-path vs Direct /30

Compare Step F series `R_k = btest(k → fiber)` and Step G `D_k = direct /30 (k ↔ k+1)`.

## Definitions

- `R_k` — tcp 10s both avg Mbps from hop `k` router to fiber router (leftover throughput through full path from that hop onward, includes all links downstream of k).
- `D_k` — tcp 5s both avg Mbps directly between `k` and `k+1` over their OSPF /30 neighbor IPs (isolates the single wireless link).

## Expected relationship

If link `k↔k+1` is the bottleneck, then `R_{k+1} - R_k` ≈ `D_k` (throughput gained by removing that link). In healthy network with spare capacity, `D_k >> (R_{k+1} - R_k)` because fiber/path limit is upstream, not that link.

## Thresholds (tightened vs analyzer)

- `D_k` is gold for single-link health: `D_k < 60%` of neighboring links' median → link degraded.
- Variance flag (unexplainable): `|D_k - (R_{k+1} - R_k)| / D_k > 40%` AND sign inconsistency (e.g., `R_{k+1} < R_k` but `D_k` high) → flag to investigate further (what NOC tech does: sniff, check wireless, look at interface errors).
- Full-path drop: `R_k` drop >50% vs `R_{k+1}` → hop `k` critical regardless of `D_k` (CPU, interface util, etc.) — same 50% rule but applied to live R series.

## CPU caveat

Direct `D_k` at `10.0.1.1↔10.0.2.1` hit 919Mbps at 94% local CPU vs `R` baseline ~64Mbps — generation bound. When `local-cpu>90%` or `remote>80%`, note "CPU-limited — generation/termination bound, not forwarding" in comment, don't declare link fantastic based on inflated `D_k`; rely primarily on `R` delta for bottleneck call.

## Examples (Zeta tower)

- `R_tower=64.1Mbps` (tower→fiber), `R_backhauls=46.3Mbps` (next hop → fiber) — delta negative -17.8Mbps (tower slightly better than next hop — within noise, not a bottleneck; maybe tower has extra CPU).
- `D_tower↔backhauls=919Mbps` → huge D but CPU-limited → not a real link bottleneck; variance not flagged as unexplained because sign consistent with fiber being limiter (R values small vs D large).

Real bad link would be: `R_k=12Mbps`, `R_{k+1}=110Mbps` (drop 89% at k), `D_k=18Mbps` → D confirms link bottleneck (both agree). Bad but with variance: `R_k=12`, `R_{k+1}=110`, `D_k=300` → D says link fine but full-path says link is bottleneck → unexplainable → fetch `interface print detail`, `log-recent`, `wireless-health`.

## Action

- variance OK → just note both numbers in hop line.
- variance flagged → auto-invoke `wireless-health` for that pair and append section to same unified comment (v4 single-comment rule).

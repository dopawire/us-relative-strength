"""Full-history chain-linked group RS-lines vs the benchmark (rotation view)."""

from collections import defaultdict

from rs_engine.config import is_discontinuity


def compute_rotation(universe, cache, bench, covered_dates, level="sector"):
    """Full-history RS line per group (chain-linked equal-weight index vs the
    benchmark, both rebased to 1.0 at the first covered date).  Handles stocks
    listing/delisting mid-history via daily cross-sectional returns; a data
    discontinuity (is_discontinuity: late split adjustment…) is not
    performance and is left out — chain-linking would make it permanent.  Returns
    {"dates": [...], "groups": [{"name", "rs"} ...]} for the rotation view."""
    by_group = defaultdict(list)
    for u in universe:
        ser = cache.get(u["sym"])
        if ser and u.get(level):
            by_group[u[level]].append(ser)

    def chain(series):
        idx = [100.0]
        for i in range(1, len(covered_dates)):
            prev, cur = covered_dates[i - 1], covered_dates[i]
            rets = [r - 1 for r in (s[cur] / s[prev] for s in series
                                    if s.get(prev) and s.get(cur))
                    if not is_discontinuity(r)]
            idx.append(idx[-1] * (1 + (sum(rets) / len(rets) if rets else 0.0)))
        return idx

    bench_idx = chain([bench])
    groups = []
    for name, series in sorted(by_group.items()):
        if len(series) < 5:
            continue
        gi = chain(series)
        rs = [round(gi[i] / bench_idx[i], 4) for i in range(len(covered_dates))]
        groups.append({"name": name, "rs": rs})
    return {"dates": covered_dates, "groups": groups}


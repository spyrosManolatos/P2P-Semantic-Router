"""
The c-ladder figure: how the correlated-failure penalty behaves as c = K/N falls.

Reads the PAIRED disaster runs (same artifact K=5500, same 500 production
queries, same targeted cluster 5495, same ring draw per rung -- only N moves)
and emits the three-panel figure for chapter 6.

Panel layout follows the argument, not the data dump:

  left    post-failure recall, one line per architecture. The two arms converge
          as c falls. This is the observation.
  middle  the gap between them in percentage points. It collapses 10.4 -> 1.8.
          Read alone, this panel says "placement stops mattering".
  right   recall lost per killed cluster, semantic / clustered. It never
          approaches 1 (parity) at any rung, in either draw -- so the gap in the
          middle panel closing is NOT placement ceasing to matter; it is the
          victim holding less. This panel is what stops the middle one from
          being over-read, so the two must never be shown apart. Its MAGNITUDE,
          however, is draw-sensitive (2.4x-5.3x across two draws at the same
          rung): read it for its sign and its distance from 1, never as a
          measured value.

x is c on a log scale, DESCENDING, so the reader travels the way the thesis
argues -- from the sparse regime the thesis measured (c=1100) toward the dense
regime a deployment would actually run (c -> 1).

Deliberately NOT plotted: the chapter's sparse-ring measurement (Table 6.8).
It sits at the same c=1100 but uses the 50-query set and picks its epicentre
from the first query, so its magnitudes are not interchangeable with these.

Styling deliberately mirrors plot.py -- the script that produced every other
figure in the chapter -- so this figure is indistinguishable from its
neighbours: same seaborn-whitegrid theme, same rcParams, same blue-circle /
green-square architecture colours, same English labels.

    python -m src.benchmarks.containerized.plot_c_ladder
    python -m src.benchmarks.containerized.plot_c_ladder --draw2   # overlay repeat draws
"""
import os
import json
import argparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

K = 5500
RUNGS = [5, 25, 100]

plt.style.use("seaborn-v0_8-whitegrid"
              if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
plt.rcParams.update({
    "font.size": 11,
    "axes.labelsize": 12,
    "axes.titlesize": 13,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "figure.titlesize": 14,
    "font.family": "sans-serif",
})

# plot.py's architecture colours: Semantic = blue circles, Clustered = green squares.
STYLE = {
    "semantic":  dict(color="b", marker="o", linestyle="-",  label="Semantic Router"),
    "clustered": dict(color="g", marker="s", linestyle="-.", label="Clustered DHT"),
}
# The middle and right panels are derived from BOTH arms, so they take a neutral
# colour rather than borrowing either architecture's.
DERIVED = "purple"


def load(results_dir, arch, n, suffix=""):
    """One rung, one arm. Returns the quantities the three panels need."""
    path = os.path.join(results_dir, f"prod.{arch}.N{n}{suffix}.json")
    if not os.path.exists(path):
        return None
    d = json.load(open(path))
    base, dis = d["baseline_recall"], d["disaster_recall"]
    killed = d["region_summary"]["killed_cluster_count"]
    return {
        "n": n,
        "c": K / n,
        "baseline": base,
        "disaster": dis,
        "killed": killed,
        "affected": d["concentration"]["n_affected"],
        # Recall lost per cluster destroyed: normalises the outcome by the size
        # of the wound, which is what makes the two arms comparable at all.
        "drop_per_killed": (base - dis) / killed,
    }


def series(results_dir, arch, suffix=""):
    return [r for r in (load(results_dir, arch, n, suffix) for n in RUNGS) if r]


def _annotate(ax, xs, ys, texts, offsets):
    """Label every point, with the vertical offset chosen PER POINT: a label
    must sit on the side the line does not arrive from, which depends on the
    slope and so cannot be derived from the index alone."""
    for x, y, t, dy in zip(xs, ys, texts, offsets):
        ax.annotate(t, xy=(x, y), xytext=(0, dy), textcoords="offset points",
                    ha="center", va="bottom" if dy > 0 else "top", fontsize=10)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    p = argparse.ArgumentParser()
    p.add_argument("--results", default=os.path.join(
        root, "data", "benchmarks", "results", "containerized", "ladder"))
    p.add_argument("--out", default=os.path.join(
        root, "data", "benchmarks", "plots", "containerized", "c_ladder.png"))
    p.add_argument("--draw2", action="store_true",
                   help="Overlay the repeat ring draw (files *.draw2.json) as "
                        "open markers, so draw-to-draw spread is visible rather "
                        "than asserted.")
    args = p.parse_args()

    sem, clu = series(args.results, "semantic"), series(args.results, "clustered")
    if not sem or not clu:
        raise SystemExit(f"no paired results under {args.results}")
    if len(sem) != len(clu):
        raise SystemExit("arms have different rung counts; the ladder must stay paired")

    cs = [r["c"] for r in sem]
    fig, axs = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle("Correlated-Failure Penalty vs Clusters per Node (c = K/N)",
                 fontsize=15, fontweight="bold")

    # ---- left: post-failure recall -----------------------------------------
    axs[0].axhline(y=sem[0]["baseline"], color="r", linestyle="--",
                   label="Pre-failure baseline")
    for arch, rows in (("semantic", sem), ("clustered", clu)):
        axs[0].plot(cs, [r["disaster"] for r in rows], markersize=8, **STYLE[arch])
    axs[0].set_ylabel("Recall after failure")
    axs[0].set_title("Post-Failure Recall")
    axs[0].set_ylim(0.25, 0.72)

    # ---- middle: the gap ----------------------------------------------------
    gap = [(c["disaster"] - s["disaster"]) * 100 for s, c in zip(sem, clu)]
    axs[1].plot(cs, gap, marker="D", linestyle="-", color=DERIVED, markersize=8,
                label="Clustered - Semantic")
    # The last point is approached by a steep descent from the left, so its
    # label goes BELOW; the others sit above a shallow segment.
    _annotate(axs[1], cs, gap, [f"{g:.1f}" for g in gap], [10, 10, -10])
    axs[1].axhline(y=0, color="r", linestyle="--", label="Parity")
    axs[1].set_ylabel("Recall gap (percentage points)")
    axs[1].set_title("The Gap Collapses")
    axs[1].set_ylim(-1.5, 14)

    # ---- right: the gap PER UNIT OF DAMAGE ---------------------------------
    ratio = [s["drop_per_killed"] / c["drop_per_killed"] for s, c in zip(sem, clu)]
    axs[2].plot(cs, ratio, marker="^", linestyle="-", color=DERIVED, markersize=9,
                label="Semantic / Clustered")
    # Mirror image of the middle panel: the first point is the foot of a steep rise.
    _annotate(axs[2], cs, ratio, [f"{r:.1f}x" for r in ratio], [-10, 10, 10])
    axs[2].axhline(y=1.0, color="r", linestyle="--", label="Parity")
    axs[2].set_ylabel("Recall lost per killed cluster (ratio)")
    axs[2].set_title("Per Unit of Damage: No Collapse")
    axs[2].set_ylim(0, 6.5)

    # Optional second draw, plotted as open markers on the same axes so the
    # reader sees the spread instead of taking a stated error bar on trust.
    if args.draw2:
        sem2 = series(args.results, "semantic", ".draw2")
        clu2 = series(args.results, "clustered", ".draw2")
        if sem2 and clu2 and len(sem2) == len(clu2):
            cs2 = [r["c"] for r in sem2]
            for arch, rows in (("semantic", sem2), ("clustered", clu2)):
                axs[0].plot(cs2, [r["disaster"] for r in rows], linestyle="none",
                            marker=STYLE[arch]["marker"], markersize=8,
                            markerfacecolor="none", markeredgewidth=1.6,
                            markeredgecolor=STYLE[arch]["color"],
                            label=f"{STYLE[arch]['label']} (2nd ring draw)")
            axs[1].plot(cs2, [(c["disaster"] - s["disaster"]) * 100
                              for s, c in zip(sem2, clu2)],
                        linestyle="none", marker="D", markersize=8,
                        markerfacecolor="none", markeredgewidth=1.6,
                        markeredgecolor=DERIVED, label="2nd ring draw")
            axs[2].plot(cs2, [s["drop_per_killed"] / c["drop_per_killed"]
                              for s, c in zip(sem2, clu2)],
                        linestyle="none", marker="^", markersize=9,
                        markerfacecolor="none", markeredgewidth=1.6,
                        markeredgecolor=DERIVED, label="2nd ring draw")
        else:
            print("  --draw2 requested but no complete paired *.draw2.json set found")

    # Both series rise left-to-right in panel 1 and fall in panel 2, so the free
    # corner differs per panel; "best" picks one that covers a marker.
    for ax, loc in zip(axs, ("lower right", "center left", "upper left")):
        ax.set_xscale("log")
        ax.invert_xaxis()          # c descends left->right: sparse -> dense
        ax.margins(x=0.14)
        ax.set_xticks(cs)
        ax.set_xticklabels([f"{c:.0f}" for c in cs])
        ax.minorticks_off()
        ax.set_xlabel("c = K/N (clusters per node)")
        ax.legend(loc=loc)
        ax.grid(True)
        secondary = ax.secondary_xaxis("top")
        secondary.set_xticks(cs)
        secondary.set_xticklabels([f"N={r['n']}" for r in sem], fontsize=10)
        secondary.tick_params(length=0)
        secondary.minorticks_off()

    fig.tight_layout(rect=[0, 0, 1, 0.93])
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    fig.savefig(args.out, dpi=150, bbox_inches="tight")
    pdf = os.path.splitext(args.out)[0] + ".pdf"
    fig.savefig(pdf, bbox_inches="tight")
    print(f"wrote {args.out}\nwrote {pdf}")

    print(f"\n{'N':>5} {'c':>7} {'sem':>7} {'clu':>7} {'gap pp':>7} "
          f"{'k_sem':>6} {'k_clu':>6} {'ratio':>6}")
    for s, c, g, r in zip(sem, clu, gap, ratio):
        print(f"{s['n']:>5} {s['c']:>7.0f} {s['disaster']:>7.3f} {c['disaster']:>7.3f} "
              f"{g:>7.2f} {s['killed']:>6} {c['killed']:>6} {r:>6.2f}")


if __name__ == "__main__":
    main()

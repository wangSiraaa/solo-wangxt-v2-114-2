"""
Per-plot and per-individual timelines over a survey sequence.

The timeline is assembled ONLY from adjacent-interval artefacts
(IntervalLink + IdentityConflict). Two non-adjacent campaigns are never
joined: where a chain link is missing or pending, the individual's line is
drawn broken with a "verify" marker instead of continuous survival.
"""
from collections import defaultdict

from inventory.models import (
    IntervalLink,
    PENDING_LINK_KINDS,
    GAP_LINK_KINDS,
    SequenceMembership,
    SurveyInterval,
    TreeMeasurement,
)


def _ordered_campaigns(sequence):
    return [m.campaign for m in sorted(
        sequence.memberships.select_related("campaign"),
        key=lambda m: m.position)]


def _intervals_map(sequence):
    ivs = sorted(
        SurveyInterval.objects.filter(sequence=sequence)
        .select_related("t1_campaign", "t2_campaign"),
        key=lambda iv: iv.ordinal)
    by_end = {iv.t2_campaign_id: iv for iv in ivs}
    return ivs, by_end


def _measurements_by_tree(campaign_ids):
    qs = TreeMeasurement.objects.filter(
        campaign_id__in=campaign_ids
    ).select_related("tree", "tree__plot", "campaign")
    by_tree = defaultdict(list)
    for m in qs:
        by_tree[m.tree_id].append(m)
    for ms in by_tree.values():
        ms.sort(key=lambda m: m.campaign.measured_on)
    return by_tree


def plot_timeline(sequence, plot):
    """Every individual in a plot: one column per campaign, one edge per link."""
    campaigns = _ordered_campaigns(sequence)
    camp_ids = [c.id for c in campaigns]
    ivs, by_end = _intervals_map(sequence)
    measurements = _measurements_by_tree(camp_ids)

    links = (IntervalLink.objects.filter(
        interval__in=ivs, plot=plot,
    ).select_related("interval", "t1_tree", "t2_tree"))
    links_by_t2_tree = defaultdict(list)
    links_by_t1_tree = defaultdict(list)
    for l in links:
        if l.t2_tree_id:
            links_by_t2_tree[l.t2_tree_id].append(l)
        if l.t1_tree_id:
            links_by_t1_tree[l.t1_tree_id].append(l)

    tree_ids = set()
    for m_list in measurements.values():
        for m in m_list:
            if m.tree.plot_id == plot.id:
                tree_ids.add(m.tree_id)

    individuals = []
    for tree_id in sorted(tree_ids):
        ms = [m for m in measurements.get(tree_id, [])
              if m.tree.plot_id == plot.id]
        by_campaign = {m.campaign_id: m for m in ms}
        nodes = []
        for c in campaigns:
            m = by_campaign.get(c.id)
            nodes.append(None if m is None else {
                "campaign": c.code,
                "measurement_id": m.id,
                "field_number": m.field_number_seen,
                "status": m.status,
                "dbh_cm": m.dbh_cm,
                "height_m": m.height_m,
                "x_m": m.x_m, "y_m": m.y_m,
            })
        edges = []
        for iv in ivs:
            edge = {"interval": iv.code, "ordinal": iv.ordinal,
                    "interval_status": iv.status,
                    "kind": None, "pending": False, "gap": False,
                    "verified": False, "excluded": False, "detail": None,
                    "conflict": None}
            link = next(
                (l for l in links_by_t2_tree.get(tree_id, [])
                 if l.interval_id == iv.id),
                next((l for l in links_by_t1_tree.get(tree_id, [])
                      if l.interval_id == iv.id), None))
            if link is not None:
                edge.update({
                    "kind": link.kind,
                    "pending": link.kind in PENDING_LINK_KINDS,
                    "gap": link.kind in GAP_LINK_KINDS,
                    "verified": link.determination
                                == IntervalLink.DETERMINED_HUMAN,
                    "excluded": link.excluded_from_components,
                    "detail": link.detail,
                    "conflict": link.conflict_id,
                })
            else:
                # No link row for this individual on this chain edge:
                # present at neither end, or link not built yet.
                edge["kind"] = "not_covered"
                if iv.status != "built":
                    edge["detail"] = {"reason": "interval not built yet"}
            edges.append(edge)

        individuals.append({
            "tree_id": tree_id,
            "current_field_number": ms[-1].tree.current_field_number
                                    if ms else "",
            "species": ms[0].tree.species.code if ms else None,
            "nodes": nodes,
            "edges": edges,
            "chain_complete": all(
                (not e["pending"] and not e["gap"]) for e in edges
                if e["kind"] not in (None, "not_covered")),
        })

    return {
        "scope": "plot",
        "sequence": sequence.code,
        "plot": plot.code,
        "campaigns": [
            {"code": c.code, "measured_on": c.measured_on.isoformat()}
            for c in campaigns],
        "intervals": [
            {"id": iv.id, "code": iv.code, "ordinal": iv.ordinal,
             "status": iv.status,
             "n_pending_links": (iv.coverage_snapshot or {})
                 .get("n_pending_links")}
            for iv in ivs],
        "individuals": individuals,
    }


def tree_timeline(sequence, tree):
    """A single individual's chain across the whole sequence."""
    data = plot_timeline(sequence, tree.plot)
    ind = next((i for i in data["individuals"]
                if i["tree_id"] == tree.id), None)
    return {
        "scope": "tree",
        "sequence": sequence.code,
        "plot": tree.plot.code,
        "tree_id": tree.id,
        "current_field_number": tree.current_field_number,
        "campaigns": data["campaigns"],
        "intervals": data["intervals"],
        "individual": ind,
    }

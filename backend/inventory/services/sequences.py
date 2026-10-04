"""
Survey sequences and adjacent-interval chain management.

A chain is built ONLY from adjacent campaigns:
2019 -> 2024 -> 2029 produces two SurveyInterval rows, each holding its own
coverage snapshot, its own IntervalLink identity verdicts, and its own
EstimateVersion editions.

* create_sequence / add_campaigns — create or extend a chain; existing links
  and confirmed editions are never rewritten.
* refresh_interval — idempotently (re)scan conflicts, re-run the strict-gap
  matcher, and rebuild the IntervalLink set + coverage fingerprint for one
  interval. Rebuilding links never mutates a frozen EstimateVersion payload.
* run_interval_estimate — a STRICT estimate edition attached to the link.
* interval_provenance — full source listing of one link.
"""
import hashlib
import json
from collections import Counter, defaultdict

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from inventory.models import (
    CONFLICT_DISTINCT,
    CONFLICT_OPEN,
    CONFLICT_RENUMBER,
    Campaign,
    HINT_GAP_REAPPEARANCE,
    HINT_LABEL_EXTRA_ROW,
    HINT_POSSIBLE_RENUMBER,
    HINT_SAME_NUMBER_MISMATCH,
    IdentityConflict,
    INTERVAL_BUILT,
    INTERVAL_PENDING,
    IntervalLink,
    LINK_BELOW_RECRUITMENT,
    LINK_DEAD_AT_T1,
    LINK_INGROWTH,
    LINK_MORTALITY,
    LINK_MISSING_T2,
    LINK_NOT_MEASURED,
    LINK_PENDING_GAP,
    LINK_PENDING_RENUMBER,
    LINK_PENDING_SAME_NUMBER,
    LINK_RECRUIT_DISTINCT,
    LINK_REMOVED_UNOBSERVED,
    LINK_RENUMBER,
    LINK_SAME_NUMBER,
    LINK_VERIFIED_GAP,
    LINK_ZERO_GROWTH,
    PENDING_LINK_KINDS,
    Plot,
    STATUS_ALIVE_MEASURED,
    STATUS_ALIVE_NOT_MEASURED,
    STATUS_DEAD,
    STATUS_MISSING,
    SurveyInterval,
    SurveySequence,
    SequenceMembership,
    TreeMeasurement,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.estimator import (
    build_measurement_table,
    earlier_known_tree_ids,
    equation_checksum,
    estimate,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements


# ---------------------------------------------------------- sequence lifecycle
@transaction.atomic
def create_sequence(code, name, campaign_codes, status="active"):
    """
    Create a sequence and its ordered adjacent intervals. Campaigns are
    ordered by measured_on regardless of the order given. Existing intervals
    are kept; only missing links are added.
    """
    campaigns = list(Campaign.objects.filter(code__in=campaign_codes))
    found = {c.code for c in campaigns}
    missing = sorted(set(campaign_codes) - found)
    if missing:
        raise ValueError(f"unknown campaigns: {missing}")
    if len(campaigns) < 2:
        raise ValueError("a sequence needs at least two campaigns")
    campaigns.sort(key=lambda c: c.measured_on)

    seq = SurveySequence.objects.filter(code=code).first()
    created = seq is None
    if seq is None:
        seq = SurveySequence.objects.create(code=code, name=name, status=status)
    for pos, camp in enumerate(campaigns):
        SequenceMembership.objects.get_or_create(
            sequence=seq, campaign=camp, defaults={"position": pos})

    _sync_membership_positions(seq)
    _ensure_intervals(seq)
    return seq, created


@transaction.atomic
def add_campaigns(sequence, campaign_codes):
    """
    Append (or insert) campaigns into a chain and create only the NEW
    adjacent intervals. Already-built intervals and their estimate editions
    are left untouched.
    """
    campaigns = list(Campaign.objects.filter(code__in=campaign_codes))
    found = {c.code for c in campaigns}
    missing = sorted(set(campaign_codes) - found)
    if missing:
        raise ValueError(f"unknown campaigns: {missing}")
    existing = set(sequence.campaigns.values_list("code", flat=True))
    new_camps = [c for c in campaigns if c.code not in existing]
    if not new_camps:
        return {"added": [], "new_intervals": []}
    # Create through rows explicitly — M2M .add() would insert a membership
    # without the NOT NULL position. Positions are re-synced from dates next.
    for pos, camp in enumerate(new_camps):
        SequenceMembership.objects.get_or_create(
            sequence=sequence, campaign=camp,
            defaults={"position": 10_000 + pos})
    _sync_membership_positions(sequence)
    intervals = _ensure_intervals(sequence)
    return {
        "added": sorted(c.code for c in new_camps),
        "new_intervals": [iv.code for iv in intervals
                          if iv.status == INTERVAL_PENDING],
    }


def _sync_membership_positions(seq):
    memberships = list(seq.memberships.select_related("campaign"))
    memberships.sort(key=lambda m: m.campaign.measured_on)
    for pos, m in enumerate(memberships):
        if m.position != pos:
            m.position = pos
            m.save(update_fields=["position"])


def _ensure_intervals(seq):
    """Create the adjacent (c_i, c_i+1) interval links that are missing."""
    camps = [m.campaign for m in sorted(
        seq.memberships.select_related("campaign"),
        key=lambda m: m.position)]
    out = []
    for i in range(len(camps) - 1):
        iv, _ = SurveyInterval.objects.get_or_create(
            sequence=seq, t1_campaign=camps[i], t2_campaign=camps[i + 1],
            defaults={"ordinal": i, "status": INTERVAL_PENDING})
        if iv.ordinal != i:
            iv.ordinal = i
            iv.save(update_fields=["ordinal"])
        out.append(iv)
    return out


def intervals_touching_campaign(campaign):
    """Every chain link having ``campaign`` as one of its ends."""
    return list(SurveyInterval.objects.filter(
        models_q_campaign(campaign)
    ).select_related("sequence"))


def models_q_campaign(campaign):
    from django.db.models import Q
    return Q(t1_campaign=campaign) | Q(t2_campaign=campaign)


@transaction.atomic
def refresh_adjacent_intervals(campaign):
    """
    After an import of one campaign, build/refresh only the adjacent links
    across all sequences containing it. Idempotent: re-uploading the same
    campaign re-runs the same scans and rebuilds the same links.
    """
    results = []
    for iv in intervals_touching_campaign(campaign):
        results.append(refresh_interval(iv))
    return results


# ------------------------------------------------------------- link materialise
def _measurement_rows(campaign, t2_campaign=None):
    qs = TreeMeasurement.objects.filter(
        campaign=campaign
    ).select_related("tree", "tree__plot", "tree__species",
                     "tree__superseded_tree")
    rows = []
    for m in qs:
        rows.append({
            "measurement_id": m.id,
            "tree_id": m.tree_id,
            "plot": m.tree.plot.code,
            "plot_id": m.tree.plot_id,
            "species": m.tree.species.code,
            "field_number": m.field_number_seen,
            "x_m": m.x_m, "y_m": m.y_m,
            "status": m.status,
            "dbh_cm": m.dbh_cm,
            "height_m": m.height_m,
            "verified_renumber_of": (
                m.tree.superseded_tree_id if campaign == t2_campaign else None
            ),
        })
    return rows


def _conflict_map(interval):
    """IdentityConflict rows of this interval, keyed for the matcher output."""
    by_pair, by_t2 = {}, {}
    for c in IdentityConflict.objects.filter(
        t1_campaign=interval.t1_campaign, t2_campaign=interval.t2_campaign,
    ).select_related("t1_measurement", "t2_measurement"):
        t1t = c.t1_measurement.tree_id if c.t1_measurement_id else None
        by_pair[(t1t, c.t2_measurement.tree_id)] = c
        by_t2.setdefault(c.t2_measurement.tree_id, []).append(c)
    return by_pair, by_t2


@transaction.atomic
def refresh_interval(interval):
    """
    (Re)scan conflicts and rebuild coverage snapshot + identity links for ONE
    interval. Strict gap chain is always on here: identity only holds inside
    this link and a missing/relabelled/contradictory tree becomes a pending
    chain item rather than a stitched survivor.
    """
    t1, t2 = interval.t1_campaign, interval.t2_campaign
    scanned = scan_conflicts(t1, t2)

    t1_rows = _measurement_rows(t1, t2_campaign=t2)
    t2_rows = _measurement_rows(t2, t2_campaign=t2)
    renumber, distinct, gap = resolved_identity_pairs(t1, t2, include_gap=True)
    earlier = earlier_known_tree_ids(t1, t2)

    pairing = pair_measurements(
        t1_rows, t2_rows,
        resolved_renumber_pairs=renumber,
        resolved_distinct_pairs=distinct,
        resolved_gap_pairs=gap,
        earlier_tree_ids=earlier,
        strict_gap_chain=True,
    )
    cfl_by_pair, cfl_by_t2 = _conflict_map(interval)

    recruitment = settings.RECRUITMENT_DBH_CM
    zero_tol = settings.ZERO_GROWTH_TOL_CM

    # Wipe and rebuild this interval's links only.
    interval.links.all().delete()
    links_spec = _build_link_specs(
        interval, pairing, cfl_by_pair, cfl_by_t2, recruitment, zero_tol)

    objs = []
    for spec in links_spec:
        conflict_id = spec.pop("conflict_id", None)
        objs.append(IntervalLink(
            interval=interval, plot_id=spec.pop("plot_id"),
            t1_tree_id=spec.pop("t1_tree", None),
            t2_tree_id=spec.pop("t2_tree", None),
            t1_measurement_id=spec.pop("t1_measurement", None),
            t2_measurement_id=spec.pop("t2_measurement", None),
            conflict_id=conflict_id,
            **spec,
        ))
    IntervalLink.objects.bulk_create(objs)

    # Counts must be read AFTER the rebuild (the call above wiped the old
    # link set); the coverage fingerprint describes this exact link set.
    link_kind_counts = Counter(
        interval.links.values_list("kind", flat=True))
    snapshot = _coverage_snapshot(
        interval, t1_rows, t2_rows, pairing, link_kind_counts)
    interval.coverage_snapshot = snapshot
    interval.status = INTERVAL_BUILT
    interval.built_at = timezone.now()
    interval.save(update_fields=["coverage_snapshot", "status", "built_at"])

    return {
        "interval": interval.code,
        "status": interval.status,
        "n_links": len(objs),
        "n_pending": sum(v for k, v in link_kind_counts.items()
                         if k in PENDING_LINK_KINDS),
        "conflicts_scanned": len(scanned),
        "fingerprint": snapshot["fingerprint"],
    }


def _build_link_specs(interval, pairing, cfl_by_pair, cfl_by_t2,
                      recruitment, zero_tol):
    specs = []

    def add(spec):
        specs.append(spec)

    # ---- matched pairs: survivors / mortality / missing / not-measured
    for pair in pairing["pairs"]:
        r1, r2 = pair["t1"], pair["t2"]
        base = {
            "plot_id": r1["plot_id"],
            "t1_tree": r1["tree_id"], "t2_tree": r2["tree_id"],
            "t1_measurement": r1["measurement_id"],
            "t2_measurement": r2["measurement_id"],
            "t1_field_number": r1["field_number"],
            "t2_field_number": r2["field_number"],
            "detail": {"distance_m": pair["distance_m"]},
        }
        if r2["status"] == STATUS_DEAD:
            add({**base, "kind": LINK_MORTALITY,
                 "determination": IntervalLink.DETERMINED_DATA,
                 "excluded_from_components": False,
                 "detail": {**base["detail"], "agb_basis": "t1"}})
        elif r2["status"] == STATUS_MISSING:
            add({**base, "kind": LINK_MISSING_T2,
                 "determination": IntervalLink.DETERMINED_DATA,
                 "excluded_from_components": True,
                 "detail": {**base["detail"], "reason": "not located t2"}})
        elif r2["status"] == STATUS_ALIVE_NOT_MEASURED:
            add({**base, "kind": LINK_NOT_MEASURED,
                 "determination": IntervalLink.DETERMINED_DATA,
                 "excluded_from_components": False,
                 "detail": {**base["detail"],
                            "reason": "alive t2, dbh not measured (MAR "
                                      "ratio-imputed)"}})
        else:  # alive_measured both ends
            is_zero = (r1["status"] == STATUS_ALIVE_MEASURED
                       and r1["dbh_cm"] is not None
                       and r2["dbh_cm"] is not None
                       and abs(r2["dbh_cm"] - r1["dbh_cm"]) <= zero_tol)
            if is_zero:
                kind = LINK_ZERO_GROWTH
            elif pair["kind"] == "renumber":
                kind = LINK_RENUMBER
            else:
                kind = LINK_SAME_NUMBER
            # A different tag on the SAME tracked row is a field-book
            # renumber (data); joining two different rows required a human
            # conflict verdict or an explicit field-book link.
            cross_rows = r1["tree_id"] != r2["tree_id"]
            human_renumber = (
                pair["kind"] == "renumber" and (
                    cross_rows
                    or any(c.status == CONFLICT_RENUMBER
                           for c in cfl_by_t2.get(r2["tree_id"], []))))
            determination = (
                IntervalLink.DETERMINED_HUMAN if human_renumber
                else IntervalLink.DETERMINED_DATA)
            add({**base, "kind": kind, "determination": determination,
                 "excluded_from_components": False,
                 "detail": {**base["detail"],
                            "dbh_t1_cm": r1["dbh_cm"],
                            "dbh_t2_cm": r2["dbh_cm"],
                            "delta_dbh_cm": (
                                None if r1["dbh_cm"] is None
                                     or r2["dbh_cm"] is None
                                else round(r2["dbh_cm"] - r1["dbh_cm"], 3))}})

    # ---- conflict / pending / verified-gap items
    for c in pairing["conflicts"]:
        r1, r2 = c.get("t1"), c.get("t2")
        ref = r2 or r1
        hint = c.get("hint", HINT_SAME_NUMBER_MISMATCH)
        verified = bool(c.get("verified"))
        conflict_obj = cfl_by_pair.get(
            ((r1 or {}).get("tree_id"), (r2 or {}).get("tree_id")))
        if conflict_obj is None and r2 is not None:
            for cand in cfl_by_t2.get(r2["tree_id"], []):
                if cand.hint == hint:
                    conflict_obj = cand
                    break
        if hint == HINT_GAP_REAPPEARANCE:
            kind = LINK_VERIFIED_GAP if verified else LINK_PENDING_GAP
            determination = (IntervalLink.DETERMINED_HUMAN if verified
                             else IntervalLink.DETERMINED_PENDING)
        elif hint == HINT_POSSIBLE_RENUMBER:
            kind = LINK_PENDING_RENUMBER
            determination = IntervalLink.DETERMINED_PENDING
        else:
            kind = LINK_PENDING_SAME_NUMBER
            determination = IntervalLink.DETERMINED_PENDING
        add({
            "plot_id": ref["plot_id"],
            "t1_tree": (r1 or {}).get("tree_id"),
            "t2_tree": (r2 or {}).get("tree_id"),
            "t1_measurement": (r1 or {}).get("measurement_id"),
            "t2_measurement": (r2 or {}).get("measurement_id"),
            "t1_field_number": (r1 or {}).get("field_number", ""),
            "t2_field_number": (r2 or {}).get("field_number", ""),
            "kind": kind,
            "determination": determination,
            "excluded_from_components": True,
            "conflict_id": conflict_obj.id if conflict_obj else None,
            "detail": {
                "hint": hint,
                "distance_m": c.get("distance_m"),
                "gap_kind": c.get("gap_kind"),
                "reason": ("pending verification chain item"
                           if not verified
                           else "human-verified gap; kept excluded, never "
                                "counted as interval growth"),
            },
        })

    # ---- t1-only: mortality candidate / dead-at-t1 / unobserved removal
    for r1 in pairing["t1_only"]:
        base = {
            "plot_id": r1["plot_id"], "t1_tree": r1["tree_id"],
            "t2_tree": None, "t1_measurement": r1["measurement_id"],
            "t2_measurement": None,
            "t1_field_number": r1["field_number"],
            "t2_field_number": "",
            "conflict_id": None,
        }
        if r1["status"] == STATUS_DEAD:
            add({**base, "kind": LINK_DEAD_AT_T1,
                 "determination": IntervalLink.DETERMINED_DATA,
                 "excluded_from_components": True,
                 "detail": {"reason": "dead at t1; mortality belongs to the "
                                      "previous interval"}})
        elif r1["status"] == STATUS_MISSING:
            add({**base, "kind": LINK_REMOVED_UNOBSERVED,
                 "determination": IntervalLink.DETERMINED_PENDING,
                 "excluded_from_components": True,
                 "detail": {"reason": "not located at t1; removal "
                                      "unquantified"}})
        elif r1["status"] == STATUS_ALIVE_MEASURED:
            add({**base, "kind": LINK_MORTALITY,
                 "determination": IntervalLink.DETERMINED_DATA,
                 "excluded_from_components": False,
                 "detail": {"source": "t1-only", "agb_basis": "t1"}})
        else:
            add({**base, "kind": LINK_REMOVED_UNOBSERVED,
                 "determination": IntervalLink.DETERMINED_DATA,
                 "excluded_from_components": True,
                 "detail": {"reason": f"t1 status {r1['status']}, no t2"}})

    # ---- t2-only: ingrowth / below recruitment
    distinct_pairs = {
        (c.t1_measurement.tree_id, c.t2_measurement.tree_id)
        for c in cfl_by_pair.values() if c.status == CONFLICT_DISTINCT
    }
    for r2 in pairing["t2_only"]:
        is_distinct_recruit = any(
            t2 == r2["tree_id"] for _t1, t2 in distinct_pairs)
        if (r2["status"] == STATUS_ALIVE_MEASURED
                and r2["dbh_cm"] is not None
                and r2["dbh_cm"] < recruitment):
            kind, excluded = LINK_BELOW_RECRUITMENT, True
            reason = f"dbh {r2['dbh_cm']} cm < recruitment {recruitment} cm"
        elif r2["status"] == STATUS_ALIVE_MEASURED:
            kind = (LINK_RECRUIT_DISTINCT if is_distinct_recruit
                    else LINK_INGROWTH)
            excluded = False
            reason = ("ingrowth after a human-verified distinct-number "
                      "decision" if is_distinct_recruit else "ingrowth")
        else:
            kind, excluded = LINK_BELOW_RECRUITMENT, True
            reason = f"t2 status {r2['status']} — not an ingrowth stem"
        add({
            "plot_id": r2["plot_id"], "t1_tree": None,
            "t2_tree": r2["tree_id"], "t1_measurement": None,
            "t2_measurement": r2["measurement_id"],
            "t1_field_number": "",
            "t2_field_number": r2["field_number"],
            "kind": kind, "determination": (
                IntervalLink.DETERMINED_HUMAN if is_distinct_recruit
                else IntervalLink.DETERMINED_DATA),
            "excluded_from_components": excluded,
            "conflict_id": None,
            "detail": {"reason": reason, "dbh_t2_cm": r2["dbh_cm"]},
        })

    return specs


def _coverage_fingerprint(t1_rows, t2_rows):
    """Hash of the measurement coverage this link is built from."""
    def sig(rows):
        return sorted(
            (r["measurement_id"], r["tree_id"], r["status"],
             r["field_number"],
             None if r["x_m"] is None else round(r["x_m"], 3),
             None if r["y_m"] is None else round(r["y_m"], 3),
             None if r["dbh_cm"] is None else round(r["dbh_cm"], 4))
            for r in rows)
    payload = json.dumps({"t1": sig(t1_rows), "t2": sig(t2_rows)},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _coverage_snapshot(interval, t1_rows, t2_rows, pairing,
                       link_kind_counts=None):
    def per_plot(rows):
        out = defaultdict(Counter)
        for r in rows:
            out[r["plot"]][r["status"]] += 1
            out[r["plot"]]["total"] += 1
        return {p: dict(c) for p, c in sorted(out.items())}

    link_kind_counts = link_kind_counts or Counter()
    pending = sum(v for k, v in link_kind_counts.items()
                  if k in PENDING_LINK_KINDS)
    return {
        "status": INTERVAL_BUILT,
        "built_at": timezone.now().isoformat(),
        "t1_campaign": interval.t1_campaign.code,
        "t2_campaign": interval.t2_campaign.code,
        "n_t1_measurements": len(t1_rows),
        "n_t2_measurements": len(t2_rows),
        "n_pairs": len(pairing["pairs"]),
        "n_pending_links": pending,
        "t1_status_counts": dict(Counter(r["status"] for r in t1_rows)),
        "t2_status_counts": dict(Counter(r["status"] for r in t2_rows)),
        "per_plot_t1": per_plot(t1_rows),
        "per_plot_t2": per_plot(t2_rows),
        "link_kind_counts": dict(link_kind_counts),
        "fingerprint": _coverage_fingerprint(t1_rows, t2_rows),
    }


# ------------------------------------------------------------- provenance / run
def interval_provenance(interval):
    """Full source listing of one interval link."""
    t1, t2 = interval.t1_campaign, interval.t2_campaign
    t1_meas = list(TreeMeasurement.objects.filter(
        campaign=t1).select_related("tree", "tree__plot"))
    t2_meas = list(TreeMeasurement.objects.filter(
        campaign=t2).select_related("tree", "tree__plot"))

    def raw(m):
        return {
            "measurement_id": m.id,
            "tree_id": m.tree_id,
            "plot": m.tree.plot.code,
            "field_number": m.field_number_seen,
            "status": m.status,
            "x_m": m.x_m, "y_m": m.y_m,
            "dbh_raw": m.dbh_raw, "dbh_unit": m.dbh_unit, "dbh_cm": m.dbh_cm,
            "height_raw": m.height_raw, "height_unit": m.height_unit,
            "height_m": m.height_m,
            "notes": m.notes,
        }

    conflicts = [
        {
            "id": c.id, "plot": c.plot.code,
            "field_number": c.field_number, "hint": c.hint,
            "status": c.status, "distance_m": c.distance_m,
            "t1_measurement": c.t1_measurement_id,
            "t2_measurement": c.t2_measurement_id,
            "resolution_note": c.resolution_note,
            "resolved_at": c.resolved_at,
        }
        for c in IdentityConflict.objects.filter(
            t1_campaign=t1, t2_campaign=t2).select_related("plot")
    ]
    links = [
        {
            "id": l.id, "plot": l.plot_id,
            "kind": l.kind, "determination": l.determination,
            "excluded": l.excluded_from_components,
            "t1_tree": l.t1_tree_id, "t2_tree": l.t2_tree_id,
            "t1_field_number": l.t1_field_number,
            "t2_field_number": l.t2_field_number,
            "conflict": l.conflict_id, "detail": l.detail,
        }
        for l in interval.links.select_related("plot").all()
    ]
    versions = [
        {"id": v.id, "label": v.label, "status": v.status,
         "created_at": v.created_at, "confirmed_at": v.confirmed_at}
        for v in interval.estimate_versions.all()
    ]
    return {
        "interval_id": interval.id,
        "sequence": interval.sequence.code,
        "interval": interval.code,
        "ordinal": interval.ordinal,
        "interval_status": interval.status,
        "coverage": interval.coverage_snapshot,
        "t1_measurements": [raw(m) for m in t1_meas],
        "t2_measurements": [raw(m) for m in t2_meas],
        "identity_conflicts": conflicts,
        "links": links,
        "estimate_versions": versions,
    }


def run_interval_estimate(interval, equation_ids, label=None, fpc=True):
    """
    Run a STRICT gap-chain draft edition attached to this interval. The
    legacy /api/estimates/ path keeps its old (non-strict) behaviour; chain
    links always run strict so a gap can never become survivor growth.
    """
    from inventory.models import AllometricEquation, EstimateVersion

    t1, t2 = interval.t1_campaign, interval.t2_campaign
    equations_qs = AllometricEquation.objects.filter(
        id__in=equation_ids).prefetch_related("species")
    if list(equations_qs.values_list("id", flat=True)) != list(equation_ids) \
            or not equation_ids:
        raise ValueError("equation_ids invalid/empty")

    table_t1, table_t2, equations, plots, strata = build_measurement_table(
        t1, t2, equations_qs)
    renumber, distinct, gap = resolved_identity_pairs(t1, t2, include_gap=True)
    earlier = earlier_known_tree_ids(t1, t2)
    interval_years = round((t2.measured_on - t1.measured_on).days / 365.25, 3)
    design = {
        "t1_code": t1.code, "t2_code": t2.code,
        "interval_years": interval_years,
        "dbh_sd_cm": settings.DBH_MEASUREMENT_SD_CM,
        "height_sd_m": settings.HEIGHT_MEASUREMENT_SD_M,
        "zero_tol_cm": settings.ZERO_GROWTH_TOL_CM,
        "recruitment_cm": settings.RECRUITMENT_DBH_CM,
        "fpc": bool(fpc),
        "crs_epsg": settings.SURVEY_CRS_EPSG,
        "strict_gap_chain": True,
        "interval_id": interval.id,
    }
    result = estimate(
        table_t1, table_t2, equations, plots, strata, design,
        resolved_renumber_pairs=renumber,
        resolved_distinct_pairs=distinct,
        resolved_gap_pairs=gap,
        earlier_tree_ids=earlier,
        strict_gap_chain=True,
    )
    result["species_without_equation"] = sorted({
        r["species"] for r in table_t1 + table_t2
        if r["species"] not in equations})
    checksum = equation_checksum(equations)

    snap_strata = {code: {**s, "plot_codes": list(s["plot_codes"])}
                   for code, s in strata.items()}
    design_snapshot = {
        **design, "strata": snap_strata,
        "equation_ids": sorted(equation_ids),
        "equation_codes": {sp: e["code"] + "@" + e["version"]
                           for sp, e in equations.items()},
        "area_tolerance": settings.PLOT_AREA_TOLERANCE}

    version = EstimateVersion.objects.create(
        label=label or f"Draft {interval.code}",
        t1_campaign=t1, t2_campaign=t2, interval=interval,
        design_snapshot=design_snapshot, result_payload=result,
        equation_checksum=checksum)
    version.equations.set(equation_ids)
    return version

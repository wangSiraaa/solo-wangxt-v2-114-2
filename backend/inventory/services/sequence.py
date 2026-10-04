"""
Multi-period survey sequences: adjacent-interval chain management.

Design rules enforced here
==========================
* Intervals exist only between ADJACENT campaigns of a sequence. The chain
  2019 -> 2024 -> 2029 yields exactly the intervals (2019,2024) and
  (2024,2029); a direct 2019 -> 2029 pairing is never constructed.
* Every interval persists its own coverage status, identity determinations
  (IntervalIdentityLink) and estimate editions. Recomputing one interval
  never touches another interval's rows.
* Confirmed estimate versions are immutable artifacts: sync/recompute only
  ever CREATE new draft editions, and only for intervals that have none.
* Everything is idempotent: re-importing the same campaign batch, retrying
  a failed sync, or recomputing twice must not duplicate intervals,
  identity links, conflicts or estimate versions.
"""
from django.db import transaction
from django.utils import timezone

from inventory.models import (
    COVERAGE_COVERED,
    COVERAGE_PARTIAL,
    COVERAGE_PENDING,
    Campaign,
    EstimateVersion,
    IdentityConflict,
    IntervalIdentityLink,
    LINK_BELOW_RECRUITMENT,
    LINK_GAP_REAPPEARANCE,
    LINK_IDENTITY_CONFLICT,
    LINK_INGROWTH,
    LINK_MORTALITY,
    LINK_NOT_LOCATED,
    LINK_RESURRECTED,
    LINK_SURVIVOR,
    LINK_SURVIVOR_RENUMBER,
    LINK_SURVIVOR_UNMEASURED,
    MeasurementImportRow,
    PENDING_LINK_KINDS,
    Plot,
    STATUS_ALIVE_MEASURED,
    STATUS_ALIVE_NOT_MEASURED,
    STATUS_DEAD,
    STATUS_MISSING,
    SequenceCampaign,
    SurveyInterval,
    SurveySequence,
    TreeMeasurement,
    VERSION_CONFIRMED,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.estimator import (
    build_measurement_table,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements


# ------------------------------------------------------------- chain shape
def ordered_campaigns(sequence):
    return sequence.ordered_campaigns()


def adjacent_pairs(sequence):
    """Adjacent (t1, t2) campaign tuples in chain order."""
    camps = ordered_campaigns(sequence)
    return list(zip(camps, camps[1:]))


def get_or_create_sequence(name, campaign_codes=None):
    """
    Idempotent sequence creation. Returns (sequence, created).
    With ``campaign_codes`` the membership is set to exactly that ordered
    list (additional existing members are kept and appended by date).
    """
    sequence, created = SurveySequence.objects.get_or_create(name=name)
    if campaign_codes:
        sync_sequence(sequence, extra_campaign_codes=campaign_codes)
    return sequence, created


@transaction.atomic
def sync_sequence(sequence, extra_campaign_codes=None, run_estimates=False,
                  equation_ids=None, fpc=True, label_prefix="auto"):
    """
    Bring the sequence up to date (补齐调查序列):

    1. add any campaigns not yet members (ordered by measurement date);
    2. create missing adjacent intervals (never duplicates, never deletes —
       intervals that stopped being adjacent are flagged, not removed);
    3. refresh coverage + identity links + conflicts for every interval;
    4. optionally create DRAFT estimates for covered intervals that have
       no edition yet (existing editions — confirmed or draft — are left
       exactly as they are).

    Returns a summary of what was created/refreshed.
    """
    summary = {"sequence": sequence.name, "added_campaigns": [],
               "created_intervals": [], "refreshed_intervals": [],
               "created_versions": [], "skipped_intervals": []}

    # 1. membership -------------------------------------------------------
    member_ids = set(sequence.memberships.values_list("campaign_id",
                                                      flat=True))
    wanted = list(extra_campaign_codes or [])
    if not wanted:
        # default: pull in every known campaign not yet a member
        wanted = [c.code for c in Campaign.objects.exclude(id__in=member_ids)
                  .order_by("measured_on")]
    for code in wanted:
        camp = Campaign.objects.filter(code=code).first()
        if camp is None or camp.id in member_ids:
            continue
        SequenceCampaign.objects.create(
            sequence=sequence, campaign=camp,
            position=(sequence.memberships.count()))
        member_ids.add(camp.id)
        summary["added_campaigns"].append(camp.code)

    # normalise positions to measurement order (chain order follows time)
    members = list(sequence.memberships.select_related("campaign")
                   .order_by("campaign__measured_on", "campaign_id"))
    for pos, m in enumerate(members):
        if m.position != pos:
            m.position = pos
            m.save(update_fields=["position"])

    # 2. intervals --------------------------------------------------------
    adjacent = set()
    for pos, (c1, c2) in enumerate(adjacent_pairs(sequence)):
        adjacent.add((c1.pk, c2.pk))
        interval, created = SurveyInterval.objects.get_or_create(
            sequence=sequence, t1_campaign=c1, t2_campaign=c2,
            defaults={"position": pos},
        )
        if created:
            summary["created_intervals"].append(
                f"{c1.code}→{c2.code}")
        if not interval.is_adjacent:
            interval.is_adjacent = True
            interval.save(update_fields=["is_adjacent"])
    for interval in sequence.intervals.exclude(is_adjacent=False):
        key = (interval.t1_campaign_id, interval.t2_campaign_id)
        if key not in adjacent:
            # chain was re-ordered around this pair; keep the interval and
            # its editions for traceability, but it is no longer a link.
            interval.is_adjacent = False
            interval.save(update_fields=["is_adjacent"])

    # 3. refresh ----------------------------------------------------------
    for interval in sequence.intervals.select_related(
            "t1_campaign", "t2_campaign"):
        refresh_interval(interval)
        summary["refreshed_intervals"].append(
            f"{interval.t1_campaign.code}→{interval.t2_campaign.code}"
            f" [{interval.coverage}]")

    # 4. draft estimates for intervals that have none ---------------------
    if run_estimates:
        for interval in sequence.intervals.filter(is_adjacent=True):
            if interval.coverage == COVERAGE_PENDING:
                summary["skipped_intervals"].append(
                    f"{interval} — no far-end data yet")
                continue
            if interval_versions(interval).exists():
                continue  # never rewrite or duplicate existing editions
            version = run_interval_estimate(
                interval, equation_ids=equation_ids, fpc=fpc,
                label=f"{label_prefix} {interval.t1_campaign.code}→"
                      f"{interval.t2_campaign.code}")
            if version is not None:
                summary["created_versions"].append(
                    {"interval": f"{interval.t1_campaign.code}→"
                                 f"{interval.t2_campaign.code}",
                     "version_id": version.id, "status": version.status})
            else:
                summary["skipped_intervals"].append(
                    f"{interval} — no usable equations")
    return summary


# ---------------------------------------------------------------- coverage
def compute_coverage(t1_campaign, t2_campaign):
    """
    pending:  no measurements at the far end yet;
    partial:  some (but not all) plots measured at both ends;
    covered:  every plot has measurements at both ends.
    """
    plot_ids = set(Plot.objects.values_list("id", flat=True))
    if not plot_ids:
        return COVERAGE_PENDING
    t1_plots = set(TreeMeasurement.objects.filter(campaign=t1_campaign)
                   .values_list("tree__plot_id", flat=True))
    t2_plots = set(TreeMeasurement.objects.filter(campaign=t2_campaign)
                   .values_list("tree__plot_id", flat=True))
    both = t1_plots & t2_plots
    if not t2_plots:
        return COVERAGE_PENDING
    if both >= plot_ids:
        return COVERAGE_COVERED
    return COVERAGE_PARTIAL


# ------------------------------------------------------------- interval IO
@transaction.atomic
def refresh_interval(interval, scan=True):
    """
    Recompute ONE interval's derived state: coverage, identity links and
    (optionally) identity conflicts. Idempotent — links are keyed by
    (interval, tree), conflicts by (t1_measurement, t2_measurement).
    Other intervals and estimate editions are untouched.
    """
    interval.coverage = compute_coverage(interval.t1_campaign,
                                         interval.t2_campaign)
    interval.save(update_fields=["coverage"])
    conflicts = []
    if scan and interval.coverage != COVERAGE_PENDING:
        conflicts = scan_conflicts(interval.t1_campaign,
                                   interval.t2_campaign)
    links = build_identity_links(interval)
    return {"coverage": interval.coverage, "n_links": len(links),
            "conflicts": conflicts}


def recompute_interval(interval, run_estimate=False, equation_ids=None,
                       fpc=True, label=None):
    """
    API-facing recompute of a single interval. Refreshes coverage, links
    and conflicts; optionally adds a NEW draft edition. Confirmed editions
    of this or any other interval are never modified.
    """
    outcome = refresh_interval(interval)
    version = None
    if run_estimate and interval.coverage != COVERAGE_PENDING:
        version = run_interval_estimate(
            interval, equation_ids=equation_ids, fpc=fpc,
            label=label or (f"recompute {interval.t1_campaign.code}→"
                            f"{interval.t2_campaign.code} "
                            f"{timezone.now():%Y-%m-%d %H:%M}"))
    outcome["new_version_id"] = version.id if version else None
    return outcome


def run_interval_estimate(interval, equation_ids=None, fpc=True, label=None):
    """
    Create a DRAFT EstimateVersion for this interval from current data.
    Returns None when no usable equations exist. Never touches existing
    versions (the caller checks for their existence when idempotency of
    editions is required).
    """
    from inventory.models import AllometricEquation
    if equation_ids:
        eqs = AllometricEquation.objects.filter(id__in=equation_ids)
        if eqs.count() != len(equation_ids):
            raise ValueError("equation_ids invalid")
    else:
        eqs = AllometricEquation.objects.filter(status="confirmed")
    eqs = eqs.prefetch_related("species")
    if not eqs.exists():
        return None

    from inventory.views import _run_estimate_for  # late import (cycles)
    return _run_estimate_for(interval.t1_campaign, interval.t2_campaign,
                             eqs, label=label or
                             f"{interval.t1_campaign.code}→"
                             f"{interval.t2_campaign.code} interval estimate",
                             fpc=fpc, interval=interval)


# ------------------------------------------------------------ identity links
def build_identity_links(interval):
    """
    Persist the per-interval identity determination of every individual
    seen at either endpoint. Idempotent: one row per (interval, tree),
    updated in place on recompute.

    Pending kinds (gap reappearance, resurrected, identity conflict) are
    the verification chain the station must clear by hand; they are never
    auto-resolved and never counted as growth by the estimator.
    """
    t1, t2 = interval.t1_campaign, interval.t2_campaign
    if interval.coverage == COVERAGE_PENDING:
        return []

    from inventory.models import AllometricEquation
    table_t1, table_t2, _eq, _plots, _strata = build_measurement_table(
        t1, t2, AllometricEquation.objects.none())
    renumber, distinct = resolved_identity_pairs(t1, t2)
    pairing = pair_measurements(table_t1, table_t2,
                                resolved_renumber_pairs=renumber,
                                resolved_distinct_pairs=distinct)

    meas_by_key = _measurement_lookup(t1, t2)

    # tree_id -> (kind, pending, note, counterpart_id, t1_row, t2_row)
    decisions = {}

    def decide(tree_id, kind, pending=False, note="", counterpart=None,
               r1=None, r2=None):
        # a tree gets exactly one link per interval; pending contradictions
        # outrank routine classifications
        prev = decisions.get(tree_id)
        if prev and prev[1] and not pending:
            return
        decisions[tree_id] = (kind, pending, note, counterpart, r1, r2)

    for pair in pairing["pairs"]:
        r1, r2 = pair["t1"], pair["t2"]
        tid = r2["tree_id"]
        s1, s2 = r1["status"], r2["status"]
        if s2 == STATUS_DEAD:
            decide(tid, LINK_MORTALITY, r1=r1, r2=r2,
                   note="mortality observation at t2")
        elif s2 == STATUS_MISSING:
            decide(tid, LINK_NOT_LOCATED, r1=r1, r2=r2,
                   note="not located at t2 — not mortality")
        elif s2 == STATUS_ALIVE_NOT_MEASURED:
            decide(tid, LINK_SURVIVOR_UNMEASURED, r1=r1, r2=r2,
                   note="alive at t2, dbh not measured (imputed, not zero)")
        elif s2 == STATUS_ALIVE_MEASURED:
            if s1 == STATUS_ALIVE_MEASURED:
                kind = (LINK_SURVIVOR_RENUMBER if pair["kind"] == "renumber"
                        else LINK_SURVIVOR)
                note = ("verified renumber within this interval"
                        if pair["kind"] == "renumber" else "")
                decide(tid, kind, r1=r1, r2=r2, note=note)
            elif s1 == STATUS_MISSING:
                # the acceptance-critical case: missing at the previous
                # occasion, back now — a PENDING gap link, never survivor
                # growth across the gap.
                decide(tid, LINK_GAP_REAPPEARANCE, pending=True, r1=r1, r2=r2,
                       note=f"missing_tree at {t1.code}, alive at {t2.code}: "
                            "continuous survival unproven — excluded from "
                            "growth until verified")
            elif s1 == STATUS_DEAD:
                decide(tid, LINK_RESURRECTED, pending=True, r1=r1, r2=r2,
                       note=f"recorded dead at {t1.code}, alive at "
                            f"{t2.code}: identity contradiction")
            else:  # alive_not_measured at t1
                decide(tid, LINK_SURVIVOR_UNMEASURED, r1=r1, r2=r2,
                       note=f"dbh missing at {t1.code}")

    for r1 in pairing["t1_only"]:
        tid = r1["tree_id"]
        if r1["status"] == STATUS_ALIVE_MEASURED:
            decide(tid, LINK_MORTALITY, r1=r1,
                   note="no t2 record; t1 biomass enters mortality")
        else:
            decide(tid, LINK_NOT_LOCATED, r1=r1,
                   note=f"no t2 record (t1 status {r1['status']}) — "
                        "removal not quantifiable")

    from django.conf import settings
    recruit = settings.RECRUITMENT_DBH_CM
    for r2 in pairing["t2_only"]:
        tid = r2["tree_id"]
        if r2["status"] == STATUS_ALIVE_MEASURED:
            if r2["dbh_cm"] is not None and r2["dbh_cm"] < recruit:
                decide(tid, LINK_BELOW_RECRUITMENT, r2=r2,
                       note=f"dbh {r2['dbh_cm']} cm < {recruit} cm threshold")
            else:
                decide(tid, LINK_INGROWTH, r2=r2,
                       note="new individual at/above recruitment threshold")
        else:
            decide(tid, LINK_NOT_LOCATED, r2=r2,
                   note=f"t2-only record with status {r2['status']}")

    for c in pairing["conflicts"]:
        r1, r2 = c.get("t1"), c.get("t2")
        hint = c.get("hint", "same_number_position_mismatch")
        d = c["distance_m"]
        note = (f"{hint}"
                + (f", {d:.2f} m apart" if d is not None else "")
                + " — open identity conflict, excluded until verified")
        if r1 is not None:
            decide(r1["tree_id"], LINK_IDENTITY_CONFLICT, pending=True,
                   note=note, counterpart=(r2 or {}).get("tree_id"),
                   r1=r1, r2=r2)
        if r2 is not None:
            decide(r2["tree_id"], LINK_IDENTITY_CONFLICT, pending=True,
                   note=note, counterpart=(r1 or {}).get("tree_id"),
                   r1=r1, r2=r2)

    links = []
    for tid, (kind, pending, note, counterpart, r1, r2) in decisions.items():
        m1 = meas_by_key.get((t1.id, tid)) if r1 is not None else None
        m2 = meas_by_key.get((t2.id, tid)) if r2 is not None else None
        link, _created = IntervalIdentityLink.objects.update_or_create(
            interval=interval, tree_id=tid,
            defaults={
                "counterpart_tree_id": counterpart,
                "t1_measurement": m1,
                "t2_measurement": m2,
                "kind": kind,
                "pending": pending or kind in PENDING_LINK_KINDS,
                "note": note,
            },
        )
        links.append(link)
    # individuals no longer present in this interval's data lose their link
    IntervalIdentityLink.objects.filter(interval=interval).exclude(
        tree_id__in=decisions.keys()).delete()
    return links


def _measurement_lookup(t1, t2):
    qs = TreeMeasurement.objects.filter(campaign__in=[t1, t2])
    return {(m.campaign_id, m.tree_id): m for m in qs}


# ------------------------------------------------------------------ queries
def interval_versions(interval):
    """
    Editions belonging to this interval: explicitly linked ones plus
    legacy editions (interval=NULL) whose campaigns match — matched
    read-time so confirmed legacy rows are never rewritten.
    """
    from django.db.models import Q
    return EstimateVersion.objects.filter(
        Q(interval=interval)
        | Q(interval__isnull=True,
            t1_campaign=interval.t1_campaign,
            t2_campaign=interval.t2_campaign)
    ).order_by("-created_at")


def interval_summary(interval):
    """Chain-list view of one interval: coverage, components, pendings."""
    versions = list(interval_versions(interval))
    latest = next((v for v in versions if v.status == VERSION_CONFIRMED),
                  versions[0] if versions else None)
    components = None
    if latest and latest.result_payload:
        comp = latest.result_payload.get("components", {})
        net = latest.result_payload.get("net_change", {})
        components = {
            "survivor_growth_mg": comp.get("survivor_growth", {})
                                     .get("total_mg"),
            "mortality_mg": comp.get("mortality", {}).get("total_mg"),
            "ingrowth_mg": comp.get("ingrowth", {}).get("total_mg"),
            "net_change_mg": net.get("total_mg"),
            "from_version": {"id": latest.id, "label": latest.label,
                             "status": latest.status},
        }
    links = interval.identity_links.all()
    return {
        "id": interval.id,
        "t1": interval.t1_campaign.code,
        "t2": interval.t2_campaign.code,
        "interval_years": interval.interval_years,
        "coverage": interval.coverage,
        "is_adjacent": interval.is_adjacent,
        "position": interval.position,
        "pending_items": sum(1 for l in links if l.pending),
        "open_conflicts": IdentityConflict.objects.filter(
            t1_campaign=interval.t1_campaign,
            t2_campaign=interval.t2_campaign, status="open").count(),
        "n_identity_links": len(links),
        "components": components,
        "versions": [
            {"id": v.id, "label": v.label, "status": v.status,
             "created_at": v.created_at, "confirmed_at": v.confirmed_at,
             "linked": v.interval_id == interval.id}
            for v in versions
        ],
    }


def sequence_chain(sequence):
    intervals = (sequence.intervals
                 .select_related("t1_campaign", "t2_campaign")
                 .prefetch_related("identity_links")
                 .order_by("position", "id"))
    items = [interval_summary(i) for i in intervals]
    covered = [i for i in items
               if i["components"] and i["is_adjacent"]]
    cumulative = None
    if covered:
        cumulative = {
            "net_change_mg": sum(i["components"]["net_change_mg"] or 0
                                 for i in covered),
            "note": "sum of adjacent-interval net changes; never a direct "
                    "first-to-last pairing",
        }
    return {
        "id": sequence.id,
        "name": sequence.name,
        "campaigns": [
            {"code": c.code, "measured_on": c.measured_on}
            for c in ordered_campaigns(sequence)
        ],
        "intervals": items,
        "cumulative_net_change": cumulative,
    }


def interval_provenance(interval):
    """
    区间来源: where this interval's numbers come from — the imported rows
    at both ends, the persisted identity determinations, the open conflicts
    and the estimate editions (linked or legacy-matched).
    """
    out = {
        "interval": {
            "id": interval.id,
            "sequence": interval.sequence.name,
            "t1": interval.t1_campaign.code,
            "t2": interval.t2_campaign.code,
            "interval_years": interval.interval_years,
            "coverage": interval.coverage,
            "is_adjacent": interval.is_adjacent,
        },
        "sources": {},
        "identity": {"links": [], "pending": [], "open_conflicts": []},
        "versions": [],
    }
    for key, camp in (("t1", interval.t1_campaign),
                      ("t2", interval.t2_campaign)):
        rows = MeasurementImportRow.objects.filter(campaign=camp)
        out["sources"][key] = {
            "campaign": camp.code,
            "measured_on": camp.measured_on,
            "measurements": TreeMeasurement.objects
                            .filter(campaign=camp).count(),
            "import_rows_accepted": rows.filter(accepted=True).count(),
            "import_rows_rejected": rows.filter(accepted=False).count(),
            "first_import_at": (rows.order_by("created_at")
                                .values_list("created_at", flat=True)
                                .first()),
            "last_import_at": (rows.order_by("-created_at")
                               .values_list("created_at", flat=True)
                               .first()),
        }
    links = (interval.identity_links
             .select_related("tree", "tree__plot",
                             "t1_measurement", "t2_measurement"))
    for l in links:
        item = {
            "tree_id": l.tree_id,
            "plot": l.tree.plot.code,
            "label_t1": (l.t1_measurement.field_number_seen
                         if l.t1_measurement else None),
            "label_t2": (l.t2_measurement.field_number_seen
                         if l.t2_measurement else None),
            "kind": l.kind,
            "pending": l.pending,
            "note": l.note,
            "counterpart_tree_id": l.counterpart_tree_id,
        }
        out["identity"]["links"].append(item)
        if l.pending:
            out["identity"]["pending"].append(item)
    for c in IdentityConflict.objects.filter(
            t1_campaign=interval.t1_campaign,
            t2_campaign=interval.t2_campaign, status="open"):
        out["identity"]["open_conflicts"].append({
            "id": c.id, "plot_id": c.plot_id,
            "field_number": c.field_number, "distance_m": c.distance_m,
            "hint": c.resolution_note,
        })
    for v in interval_versions(interval):
        out["versions"].append({
            "id": v.id, "label": v.label, "status": v.status,
            "equation_checksum": v.equation_checksum,
            "created_at": v.created_at, "confirmed_at": v.confirmed_at,
            "linked": v.interval_id == interval.id,
        })
    return out


def plot_timeline(sequence, plot_code):
    """
    Per-individual matrix for one plot across the whole chain: each tree's
    measurement at every campaign and its identity link in every interval.
    """
    campaigns = ordered_campaigns(sequence)
    intervals = list(sequence.intervals.select_related(
        "t1_campaign", "t2_campaign").order_by("position", "id"))
    measurements = (TreeMeasurement.objects
                    .filter(campaign__in=campaigns, tree__plot__code=plot_code)
                    .select_related("tree", "campaign"))
    by_tree = {}
    for m in measurements:
        by_tree.setdefault(m.tree_id, {"tree": m.tree, "occasions": {}})
        by_tree[m.tree_id]["occasions"][m.campaign.code] = {
            "status": m.status,
            "field_number": m.field_number_seen,
            "dbh_cm": m.dbh_cm,
            "height_m": m.height_m,
        }
    links = (IntervalIdentityLink.objects
             .filter(interval__in=intervals, tree__plot__code=plot_code)
             .select_related("interval", "interval__t1_campaign",
                             "interval__t2_campaign"))
    for l in links:
        entry = by_tree.setdefault(l.tree_id, {"tree": l.tree,
                                               "occasions": {}})
        key = f"{l.interval.t1_campaign.code}→{l.interval.t2_campaign.code}"
        entry.setdefault("intervals", {})[key] = {
            "kind": l.kind, "pending": l.pending, "note": l.note,
        }
    trees = []
    for tid, entry in by_tree.items():
        tree = entry["tree"]
        trees.append({
            "tree_id": tid,
            "plot": tree.plot.code,
            "species": tree.species.code,
            "current_field_number": tree.current_field_number,
            "occasions": entry.get("occasions", {}),
            "intervals": entry.get("intervals", {}),
        })
    trees.sort(key=lambda t: t["current_field_number"])
    return {
        "sequence": sequence.name,
        "plot": plot_code,
        "campaigns": [{"code": c.code, "measured_on": c.measured_on}
                      for c in campaigns],
        "intervals": [f"{i.t1_campaign.code}→{i.t2_campaign.code}"
                      for i in intervals],
        "trees": trees,
    }


def tree_timeline(tree):
    """One individual's full chronological trace across the chain."""
    measurements = (tree.measurements.select_related("campaign")
                    .order_by("campaign__measured_on"))
    links = (tree.interval_links
             .select_related("interval", "interval__t1_campaign",
                             "interval__t2_campaign")
             .order_by("interval__position"))
    return {
        "tree_id": tree.id,
        "plot": tree.plot.code,
        "species": tree.species.code,
        "current_field_number": tree.current_field_number,
        "occasions": [
            {"campaign": m.campaign.code, "measured_on": m.campaign.measured_on,
             "field_number": m.field_number_seen, "status": m.status,
             "dbh_cm": m.dbh_cm, "height_m": m.height_m,
             "x_m": m.x_m, "y_m": m.y_m}
            for m in measurements
        ],
        "interval_links": [
            {"interval": (f"{l.interval.t1_campaign.code}→"
                          f"{l.interval.t2_campaign.code}"),
             "sequence": l.interval.sequence.name,
             "kind": l.kind, "pending": l.pending, "note": l.note}
            for l in links
        ],
    }

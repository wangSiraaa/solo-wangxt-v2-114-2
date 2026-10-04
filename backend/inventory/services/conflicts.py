"""
Scan ONE adjacent campaign interval for identity items and persist them.

Every item is scoped to exactly one interval (t1_campaign, t2_campaign):
identity relations never leak across a gap, and a 2029 near-neighbour
renumber only flags the 2024->2029 link.

Item kinds:
  * same field number, contradictory position      -> same_number_mismatch
  * different number, close position               -> possible_renumber
  * t1 missing_tree/dead, t2 alive (same tree row) -> gap_reappearance
  * t2 tree row unknown at t1 but known EARLIER    -> gap_reappearance
    (2019 tree, nothing in 2024, re-found 2029)

Nothing is ever merged automatically. Scanning is idempotent: get_or_create
on the exact (t1_measurement|None, t2_measurement) pair, so retransmissions
and retries never duplicate an item.
"""
from django.utils import timezone

from inventory.models import (
    HINT_GAP_REAPPEARANCE,
    HINT_POSSIBLE_RENUMBER,
    HINT_SAME_NUMBER_MISMATCH,
    IdentityConflict,
    STATUS_ALIVE_MEASURED,
    STATUS_ALIVE_NOT_MEASURED,
    STATUS_DEAD,
    STATUS_MISSING,
)
from inventory.services.identity import (
    POSITION_TOLERANCE_M,
    RENUMBER_SEARCH_RADIUS_M,
    _distance_m,
)

_ALIVE = {STATUS_ALIVE_MEASURED, STATUS_ALIVE_NOT_MEASURED}


def scan_conflicts(t1_campaign, t2_campaign,
                   tolerance_m=POSITION_TOLERANCE_M,
                   search_radius_m=RENUMBER_SEARCH_RADIUS_M,
                   detect_gaps=True):
    from inventory.models import Campaign, TreeMeasurement

    t1_qs = list(TreeMeasurement.objects.filter(
        campaign=t1_campaign
    ).select_related("tree", "tree__plot"))
    t2_qs = list(TreeMeasurement.objects.filter(
        campaign=t2_campaign
    ).select_related("tree", "tree__plot"))

    t1_by_plot_num, t2_by_plot_num = {}, {}
    for m in t1_qs:
        t1_by_plot_num.setdefault((m.tree.plot_id, m.field_number_seen), []).append(m)
    for m in t2_qs:
        t2_by_plot_num.setdefault((m.tree.plot_id, m.field_number_seen), []).append(m)

    found = []
    # same label, contradictory position. Iterate from the t2 side so an
    # EXTRA row reusing a label is caught even when the genuine t1 tree is
    # also present at its original position.
    for key, m2_list in t2_by_plot_num.items():
        for m2 in m2_list:
            t1_list = t1_by_plot_num.get(key, [])
            if not t1_list:
                continue
            nearest = None
            for m1 in t1_list:
                d = _distance_m(m1.x_m, m1.y_m, m2.x_m, m2.y_m)
                if nearest is None or d < nearest[0]:
                    nearest = (d, m1)
            d, m1 = nearest
            if d > tolerance_m and not _has_resolution(m1, m2):
                found.append(_upsert(t1_campaign, t2_campaign, m1, m2, d,
                                     HINT_SAME_NUMBER_MISMATCH,
                                     m1.field_number_seen))

    # different labels, close position -> possible unrecorded renumber,
    # unless the two records are already the SAME tracked tree (a verified
    # tag replacement) or a human decision exists.
    t2_by_plot = {}
    for m in t2_qs:
        t2_by_plot.setdefault(m.tree.plot_id, []).append(m)
    for m1 in t1_qs:
        if m1.status == "dead":
            # a recruit beside a documented dead snag is not a renumber
            continue
        for m2 in t2_by_plot.get(m1.tree.plot_id, []):
            if m1.tree_id == m2.tree_id:
                continue
            if m1.field_number_seen == m2.field_number_seen:
                continue
            d = _distance_m(m1.x_m, m1.y_m, m2.x_m, m2.y_m)
            if d <= search_radius_m and not _has_resolution(m1, m2):
                found.append(_upsert(t1_campaign, t2_campaign, m1, m2, d,
                                     HINT_POSSIBLE_RENUMBER,
                                     m1.field_number_seen))

    if detect_gaps:
        found.extend(_scan_gap_reappearances(
            t1_campaign, t2_campaign, t1_qs, t2_qs))
    return found


def _scan_gap_reappearances(t1_campaign, t2_campaign, t1_qs, t2_qs):
    """Chain holes that must stay pending instead of survivor growth."""
    from inventory.models import TreeMeasurement

    found = []
    t1_by_tree = {m.tree_id: m for m in t1_qs}

    # Tree rows already known from a campaign strictly BEFORE this t1.
    earlier_tree_ids = set(TreeMeasurement.objects.filter(
        campaign__measured_on__lt=t1_campaign.measured_on,
    ).values_list("tree_id", flat=True).distinct())

    for m2 in t2_qs:
        if m2.status not in _ALIVE:
            continue
        m1 = t1_by_tree.get(m2.tree_id)
        if m1 is not None:
            if m1.status in (STATUS_MISSING, STATUS_DEAD):
                # alive t2 on a row that was missing/dead at t1 of THIS
                # interval -> the chain has a hole at t1.
                if not _has_resolution(m1, m2, hint=HINT_GAP_REAPPEARANCE):
                    d = _distance_m(m1.x_m, m1.y_m, m2.x_m, m2.y_m)
                    found.append(_upsert(
                        t1_campaign, t2_campaign, m1, m2, d,
                        HINT_GAP_REAPPEARANCE, m2.field_number_seen))
        elif m2.tree_id in earlier_tree_ids:
            # no record at this interval's t1 at all, but the row lived at an
            # earlier occasion (2019 -> [no 2024] -> 2029).
            if not _has_resolution(None, m2, hint=HINT_GAP_REAPPEARANCE):
                found.append(_upsert(
                    t1_campaign, t2_campaign, None, m2, None,
                    HINT_GAP_REAPPEARANCE, m2.field_number_seen))
    return found


def _has_resolution(m1, m2, hint=None):
    qs = IdentityConflict.objects.filter(t2_measurement=m2)
    if m1 is not None:
        qs = qs.filter(t1_measurement=m1)
    else:
        qs = qs.filter(t1_measurement__isnull=True)
    if hint is not None:
        qs = qs.filter(hint=hint)
    return qs.exclude(status="open").exists()


def _upsert(t1_campaign, t2_campaign, m1, m2, distance, hint, field_number):
    obj, created = IdentityConflict.objects.get_or_create(
        t1_measurement=m1, t2_measurement=m2, hint=hint,
        defaults={
            "plot": m2.tree.plot,
            "field_number": field_number,
            "t1_campaign": t1_campaign,
            "t2_campaign": t2_campaign,
            "distance_m": None if distance is None else round(distance, 3),
            "resolution_note": hint,
        },
    )
    return {"id": obj.id, "created": created,
            "plot": m2.tree.plot.code,
            "field_number": field_number,
            "t2_field_number": m2.field_number_seen,
            "distance_m": None if distance is None else round(distance, 2),
            "hint": hint if created else obj.get_status_display()}

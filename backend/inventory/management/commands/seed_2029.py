"""
Seed the FICTIONAL third remeasurement (2029) on top of seed_demo.

Run AFTER `seed_demo`:

    python3 manage.py seed_demo
    python3 manage.py seed_2029

The command is idempotent: re-running it (or re-importing the same batch)
updates measurements in place and never duplicates trees, intervals,
identity links, conflicts or estimate editions.

Scenarios covered by the 2029 batch
===================================
* ordinary survivor growth on all plots;
* P02/005: missing_tree in 2024, measured again in 2029 -> PENDING
  gap-reappearance link, never automatic survivor growth across the gap;
* P02/006 -> new tag 106 0.45 m away: possible-renumber conflict scoped to
  the 2024->2029 interval ONLY;
* P05/004: label reused 18.7 m away -> same-number position contradiction,
  open conflict in 2024->2029 only, both rows excluded;
* P01/008 interloper and P01/009 (BIR) persist: their 2019->2024 conflicts
  stay open in that interval; within 2024->2029 they are established rows;
* P05/006: missing in 2024, found dead in 2029 -> mortality timing
  unquantifiable, listed but not summed;
* P05/002: alive but not measured in 2029 (missing data, not zero);
* P04/006, P01/005, P02/003: unmeasured in 2024, measured again in 2029;
* new recruits P01/203, P01/204 -> ingrowth in 2024->2029;
* P01/201 crosses the 5 cm recruitment threshold inside the interval.
"""
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import Campaign, Plot
from inventory.services.conflicts import scan_conflicts
from inventory.services.ingest import import_campaign_rows

AM = "alive_measured"
AN = "alive_not_measured"
DE = "dead"

# (plot, field_number, species, dx, dy, status, dbh_cm, height_m)
ROWS_2029 = [
    # ---- P01 -------------------------------------------------------------
    ("P01", "001", "OAK", 10, 10, AM, 26.3, 17.2),
    ("P01", "002", "OAK", 25, 12, AM, 32.2, 19.8),
    ("P01", "003", "PIN", 40, 18, AM, 38.0, 23.4),
    ("P01", "004", "OAK", 55, 20, AM, 18.4, 13.6),
    # unmeasured in 2024 (tape failure), measured again now
    ("P01", "005", "BIR", 70, 25, AM, 13.1, 10.5),
    # 006 died before 2024 -> no row
    ("P01", "017", "OAK", 20, 40, AM, 23.8, 15.9),   # the renumbered 007
    ("P01", "008", "PIN", 15, 30, AM, 28.9, 18.8),   # original 008
    ("P01", "008", "PIN", 40, 30, AM, 20.8, 14.3),   # 2024 interloper persists
    # 009 (OAK) died before 2024 -> no row; the 2024 BIR interloper grows on
    ("P01", "009", "BIR", 62, 10, AM, 10.2, 8.6),
    ("P01", "201", "BIR", 90, 40, AM, 5.4, 5.1),     # crossed 5 cm threshold
    ("P01", "202", "BIR", 92, 15, AM, 8.9, 7.5),
    ("P01", "203", "BIR", 88, 8, AM, 6.1, 5.8),      # NEW ingrowth
    ("P01", "204", "OAK", 50, 45, AM, 5.2, 4.8),     # NEW ingrowth
    # ---- P02 -------------------------------------------------------------
    ("P02", "001", "OAK", 12, 14, AM, 28.0, 17.9),
    ("P02", "002", "PIN", 30, 20, AM, 35.6, 22.3),
    ("P02", "003", "BIR", 45, 33, AM, 10.9, 9.4),    # unmeasured 2024
    ("P02", "004", "OAK", 55, 40, AM, 22.6, 15.4),
    # GAP REAPPEARANCE: missing_tree in 2024, back in 2029 at the same spot.
    # Must become a pending link, never automatic survivor growth.
    ("P02", "005", "PIN", 60, 55, AM, 29.3, 19.0),
    # RENUMBER CANDIDATE: old tag 006 gone, new tag 106 0.45 m away, no
    # field-book link -> possible_renumber conflict for THIS interval only.
    ("P02", "106", "BIR", 20.4, 50.2, AM, 16.0, 11.9),
    ("P02", "118", "OAK", 40.4, 60.2, AM, 20.5, 13.6),
    ("P02", "201", "BIR", 65, 30, AM, 7.5, 6.9),
    # ---- P03 -------------------------------------------------------------
    ("P03", "001", "PIN", 10, 10, AM, 34.7, 21.2),
    ("P03", "002", "OAK", 20, 15, AM, 23.9, 15.9),
    ("P03", "003", "BIR", 30, 20, AM, 12.8, 10.4),
    # 004 died before 2024 -> no row
    # ---- P04 -------------------------------------------------------------
    ("P04", "001", "OAK", 15, 20, AM, 47.8, 26.0),
    ("P04", "002", "OAK", 35, 40, AM, 104.8, 35.0),  # still above dbh range
    ("P04", "003", "PIN", 55, 30, AM, 40.9, 24.2),
    ("P04", "004", "BIR", 70, 60, AM, 14.8, 11.4),
    # 005 died before 2024 -> no row
    ("P04", "006", "OAK", 25, 85, AM, 28.1, 17.9),   # unmeasured 2024
    ("P04", "201", "BIR", 90, 20, AM, 9.4, 8.1),
    # ---- P05 -------------------------------------------------------------
    ("P05", "001", "PIN", 20, 25, AM, 33.8, 21.3),
    ("P05", "002", "PIN", 40, 45, AN, None, None),   # alive, NOT measured
    ("P05", "003", "OAK", 60, 30, AM, 25.9, 16.9),
    # POSITION CONTRADICTION: label 004 reused 18.7 m from the original
    # (which gets no 2029 row) -> open conflict, both excluded this interval.
    ("P05", "004", "BIR", 93, 75, AM, 6.5, 6.0),
    ("P05", "005", "OAK", 85, 85, AM, 20.0, 14.0),   # verified zero again
    # missing in 2024, found dead now: timing unquantifiable -> listed only
    ("P05", "006", "BIR", 30, 70, DE, None, None),
    ("P05", "201", "BIR", 50, 80, AM, 6.7, 6.1),
]


class Command(BaseCommand):
    help = "Import the fictional 2029 remeasurement and extend the chain."

    @transaction.atomic
    def handle(self, *args, **options):
        from inventory.models import SurveySequence, Tree
        if not Plot.objects.exists():
            raise CommandError("run `python3 manage.py seed_demo` first")
        seq = SurveySequence.objects.filter(name="main").first()
        if seq is None:
            raise CommandError("sequence 'main' missing — run seed_demo first")

        t2 = Campaign.objects.get(code="2024")
        t3, _ = Campaign.objects.get_or_create(
            code="2029",
            defaults={"measured_on": date(2029, 7, 12),
                      "description": "Third remeasurement (fictional)"},
        )

        origins = {p.code: p.boundary[0] for p in Plot.objects.all()}
        rows = []
        for plot, num, sp, dx, dy, st, dbh, h in ROWS_2029:
            ox, oy = origins[plot]
            rows.append(dict(
                plot=plot, field_number=num, species=sp,
                x_m=ox + dx, y_m=oy + dy, status=st,
                dbh_raw=dbh, dbh_unit="cm" if dbh is not None else None,
                height_raw=h, height_unit="m" if h is not None else None,
                notes=("verified zero growth cross-check"
                       if plot == "P05" and num == "005" else ""),
            ))

        before_trees = Tree.objects.count()
        res = import_campaign_rows(t3, rows, settings.PLOT_AREA_TOLERANCE)
        self.stdout.write(
            f"2029 import: accepted={res['n_accepted']} "
            f"rejected={res['n_rejected']} "
            f"(new tree rows: {Tree.objects.count() - before_trees})")
        for bad in res["rejected"]:
            self.stdout.write("  REJECTED: " + bad["reason"])

        # identity contradictions for the 2024->2029 interval ONLY
        found = scan_conflicts(t2, t3)
        self.stdout.write(f"identity conflicts (2024→2029): {len(found)}")
        for f in found:
            self.stdout.write(
                f"  {f['plot']}/{f['field_number']} -> "
                f"{f.get('t2_field_number')} d={f['distance_m']}m "
                f"[{f['hint']}]")

        # extend the chain: 2029 joins the sequence, interval 2024->2029 is
        # created and refreshed, and a DRAFT estimate is added for the new
        # interval only. The confirmed 2019->2024 edition is untouched.
        from inventory.services.sequence import sync_sequence
        summary = sync_sequence(seq, run_estimates=True)
        self.stdout.write(f"added campaigns: {summary['added_campaigns']}")
        self.stdout.write(f"created intervals: {summary['created_intervals']}")
        self.stdout.write(f"refreshed: {summary['refreshed_intervals']}")
        self.stdout.write(f"created versions: {summary['created_versions']}")

        self.stdout.write(self.style.SUCCESS("2029 remeasurement loaded"))

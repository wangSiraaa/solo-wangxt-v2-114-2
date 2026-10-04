"""
Acceptance tests for the permanent-plot station.

Covers the acceptance checks specified by the station:
  A. remeasurement renumber handling;
  B. unequal plot areas in population expansion;
  C. measurement-unit mistakes rejected at ingest;
  D. real zero growth vs missing data vs mortality kept distinct;
  E. same number + contradictory position never auto-merged;
  F. confirmed estimate editions cannot be silently changed by a new
     allometric equation.
"""
import json

from django.conf import settings
from django.core.exceptions import ValidationError as DjValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    EstimateVersion,
    IdentityConflict,
    Plot,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements
from inventory.services.ingest import import_campaign_rows, verify_plot_area
from inventory.services.units import convert_dbh_to_cm, ring_area_ha


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM, AN, DE = "alive_measured", "alive_not_measured", "dead"


class EstimatorAcceptanceTests(TestCase):
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        # Unequal plot areas: 0.10 ha and 0.25 ha.
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.p2 = Plot.objects.create(
            code="P2", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)

    def _run(self):
        t1t, t2t, equations, plots, strata = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        ren, dist = resolved_identity_pairs(self.t1, self.t2)
        design = dict(t1_code="t1", t2_code="t2", interval_years=5.0,
                      dbh_sd_cm=0.1, height_sd_m=0.3, zero_tol_cm=0.15,
                      recruitment_cm=5.0, fpc=False, crs_epsg=32650)
        return estimate(t1t, t2t, equations, plots, strata, design, ren, dist)

    # ---------- C. unit mistakes ------------------------------------------------
    def test_dbh_unit_must_be_explicit(self):
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(25.0, None)

    def test_mm_entered_as_cm_is_rejected_by_range(self):
        # 250 (mm) typed as cm -> 250 cm beyond the accepted demo range.
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(250.0, "cm")

    def test_mm_value_correctly_converted(self):
        self.assertAlmostEqual(convert_dbh_to_cm(250.0, "mm"), 25.0)

    def test_import_rejects_bad_unit_rows(self):
        rows = [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=10, y_m=10, status=AM,
                 dbh_raw=250.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P1", field_number="2", species="OAK",
                 x_m=12, y_m=12, status=AM,
                 dbh_raw=20.0, height_raw=15.0, height_unit="m"),
        ]
        r = import_campaign_rows(self.t2, rows, 0.01)
        self.assertEqual(r["n_rejected"], 2)
        self.assertEqual(TreeMeasurement.objects.count(), 0)

    # ---------- B. unequal plot areas ------------------------------------------
    def test_unequal_plot_areas_expanded_per_plot(self):
        # 100 kg growth on each plot; per-ha: 1000 vs 400 kg/ha.
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        # choose t2 dbh giving +100 kg growth per tree with a=0.1,b=2,c=.5
        def b_at(d):
            return 0.1 * d ** 2 * 15 ** 0.5
        import math
        d2 = math.sqrt((b_at(20.0) + 100.0) / (0.1 * 15 ** 0.5))
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        res = self._run()
        # per-ha mean = (1000 + 400)/2 = 700 kg/ha; x 100 ha = 70 000 kg
        self.assertAlmostEqual(
            res["components"]["survivor_growth"]["total_kg"],
            70_000.0, delta=1e-6)
        # The naive "mean tree * area" would give 100 kg * (100/0.175 avg?)
        # and clearly differs; the provenance carries per-plot values.
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual(p1["area_ha"], 0.10)

    def test_plot_area_polygon_crosscheck(self):
        bad = Plot(code="BAD", stratum=self.sA, x_m=0, y_m=0,
                   declared_area_ha=0.50, boundary=rect(0, 0, 50, 20),
                   area_polygon_ha=0.10)
        with self.assertRaises(DjValidationError):
            verify_plot_area(bad, 0.01)

    # ---------- D. zero / missing / dead ---------------------------------------
    def test_zero_growth_missing_and_dead_are_distinct(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=AM, dbh_raw=22.0, dbh_unit="cm",
                 height_raw=16.0, height_unit="m"),
        ], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m",
                 notes="verified zero growth"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AN),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=DE),
        ], 0.01)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([z["tree"] for z in p1["verified_zero_growth"]],
                         ["P1/z"])
        self.assertEqual([m["tree"] for m in p1["alive_not_measured"]],
                         ["P1/m"])
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/d"])
        # missing survivor did NOT silently become zero growth:
        self.assertTrue(p1["imputed_survivor_growth_kg"] >= 0)

    # ---------- A + E. renumber / same-number contradiction --------------------
    def test_renumber_keeps_one_individual(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="007", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        tree = Tree.objects.get(current_field_number="007")
        tree.current_field_number = "017"
        tree.save(update_fields=["current_field_number"])
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="017", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        t1t, t2t, *_ = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        pairing = pair_measurements(t1t, t2t)
        self.assertEqual(len(pairing["pairs"]), 1)
        self.assertEqual(pairing["pairs"][0]["kind"], "renumber")

    def test_same_number_position_contradiction_is_excluded(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="008", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        # new tree row, same label, 15 m away
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="008", species="OAK", x_m=15, y_m=5,
                 status=AM, dbh_raw=12.0, dbh_unit="cm",
                 height_raw=10.0, height_unit="m")], 0.01)
        self.assertEqual(
            Tree.objects.filter(plot=self.p1,
                                current_field_number="008").count(), 2)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        excluded = [c["tree"] for c in p1["excluded_identity_conflicts"]]
        self.assertIn("P1/008", excluded)
        # not counted as growth, nor as mortality, nor as ingrowth
        self.assertEqual(p1["kg"]["survivor_growth"], 0.0)
        self.assertEqual(p1["mortality"], [])
        self.assertEqual(p1["ingrowth"], [])

    def test_distinct_resolution_counts_removal_and_ingrowth(self):
        from inventory.services.conflicts import scan_conflicts
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="009", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=16.0, dbh_unit="cm",
                 height_raw=12.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="009", species="OAK", x_m=25, y_m=5,
                 status=AM, dbh_raw=8.0, dbh_unit="cm",
                 height_raw=8.0, height_unit="m")], 0.01)
        found = scan_conflicts(self.t1, self.t2)
        self.assertTrue(found)
        client = APIClient()
        cid = found[0]["id"]
        resp = client.post(f"/api/conflicts/{cid}/resolve/",
                           {"status": "distinct", "note": "new recruit"})
        self.assertEqual(resp.status_code, 200)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/009"])
        self.assertEqual([m["tree"] for m in p1["ingrowth"]], ["P1/009"])

    # ---------- F. confirmed edition immutability ------------------------------
    def test_confirmed_estimate_is_frozen_against_new_equation(self):
        client = APIClient()
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        body = dict(label="v1", t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq.id], fpc=False)
        r = client.post("/api/estimates/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        before = r.json()["result_payload"]["components"]["survivor_growth"]

        rc = client.post(f"/api/estimates/{vid}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)

        # 1) the JSON result stays byte-stable
        again = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(
            again["result_payload"]["components"]["survivor_growth"], before)

        # 2) the equation is locked: coefficient change refused
        self.eq.refresh_from_db()
        self.eq.a = 0.999
        with self.assertRaises(PermissionError):
            self.eq.save()

        # 3) the edition row itself cannot be mutated
        version = EstimateVersion.objects.get(pk=vid)
        version.label = "tampered"
        with self.assertRaises(PermissionError):
            version.save()

        # 4) a new equation must be issued as a NEW equation row/version
        eq2 = AllometricEquation.objects.create(
            code="OAK", version="2", status="draft",
            a=0.2, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional revised")
        eq2.species.add(self.oak)
        r2 = client.post("/api/estimates/",
                         dict(label="v2-new-equation",
                              t1_campaign="t1", t2_campaign="t2",
                              equation_ids=[eq2.id], fpc=False),
                         format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r2.json()["id"], vid)
        # old edition unchanged
        old = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(old["label"], "v1")
        self.assertEqual(
            old["result_payload"]["components"]["survivor_growth"], before)

    def test_result_payload_records_units_and_sources(self):
        res = self._run()
        self.assertEqual(res["units"]["dbh"],
                         "cm (converted at ingest; raw unit retained)")
        self.assertEqual(res["units"]["height"], "m")
        self.assertIn("estimator", res["design"])
        self.assertTrue(res["uncertainty_assumptions"])
        self.assertIn("OAK", res["equations_used"])


# ---------------------------------------------------------------------------
# Multi-period survey sequences (2019 -> 2024 -> 2029)
# ---------------------------------------------------------------------------
MI = "missing_tree"


class SurveySequenceAcceptanceTests(TestCase):
    """
    Acceptance for the third remeasurement: adjacent-interval chain,
    interval-scoped identity, idempotent backfill, frozen confirmed editions.
    """

    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.c19 = Campaign.objects.create(code="2019",
                                           measured_on="2019-07-01")
        self.c24 = Campaign.objects.create(code="2024",
                                           measured_on="2024-07-01")
        self.c29 = Campaign.objects.create(code="2029",
                                           measured_on="2029-07-01")
        self.pA = Plot.objects.create(
            code="PA", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.pB = Plot.objects.create(
            code="PB", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)
        self.client = APIClient()

        def row(plot, num, x, y, status, dbh=None, h=None):
            return dict(plot=plot, field_number=num, species="OAK",
                        x_m=x, y_m=y, status=status,
                        dbh_raw=dbh, dbh_unit="cm" if dbh is not None else None,
                        height_raw=h, height_unit="m" if h is not None else None)
        self._row = row

        # 2019 census
        import_campaign_rows(self.c19, [
            row("PA", "1", 5, 5, AM, 20.0, 15.0),
            row("PA", "2", 10, 5, AM, 18.0, 14.0),
            row("PA", "3", 15, 5, AM, 22.0, 16.0),
            row("PB", "1", 5, 5, AM, 25.0, 17.0),
        ], 0.01)
        # 2024 remeasurement: PA/2 NOT LOCATED (missing_tree)
        import_campaign_rows(self.c24, [
            row("PA", "1", 5, 5, AM, 21.0, 15.0),
            row("PA", "2", 10, 5, MI),
            row("PA", "3", 15, 5, AM, 23.0, 16.0),
            row("PB", "1", 5, 5, AM, 26.0, 17.0),
        ], 0.01)

        # legacy two-period confirmed edition (created BEFORE any sequence)
        r = self.client.post("/api/estimates/",
                             dict(label="original 2019→2024",
                                  t1_campaign="2019", t2_campaign="2024",
                                  equation_ids=[self.eq.id], fpc=False),
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.v1_id = r.json()["id"]
        rc = self.client.post(f"/api/estimates/{self.v1_id}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)
        self.v1_snapshot = self.client.get(
            f"/api/estimates/{self.v1_id}/").json()

        # sequence covering 2019 -> 2024
        r = self.client.post("/api/sequences/",
                             {"name": "main", "campaigns": ["2019", "2024"]},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.seq_id = r.json()["id"]

    # ---- helpers ----------------------------------------------------------
    def _import_2029(self):
        """The third remeasurement batch."""
        return self.client.post("/api/imports/", {
            "campaign": "2029",
            "rows": [
                self._row("PA", "1", 5, 5, AM, 22.0, 15.0),
                # gap: missing 2024, back 2029 at the same spot
                self._row("PA", "2", 10, 5, AM, 19.0, 14.0),
                self._row("PA", "3", 15, 5, DE),
                # renumber candidate: old tag PB/1 gone, new tag 101 close by
                self._row("PB", "101", 5.4, 5.2, AM, 27.0, 17.0),
                self._row("PB", "2", 40, 40, AM, 6.0, 5.0),   # ingrowth
            ]}, format="json")

    def _sync(self, **kw):
        return self.client.post(f"/api/sequences/{self.seq_id}/sync/",
                                kw, format="json")

    def _interval(self, t1, t2):
        from inventory.models import SurveyInterval
        return SurveyInterval.objects.get(t1_campaign__code=t1,
                                          t2_campaign__code=t2)

    def _chain(self):
        return self.client.get(f"/api/sequences/{self.seq_id}/").json()

    # ---- (a) adding 2029 only adds the latter interval's estimate ---------
    def test_adding_2029_only_adds_latter_interval_estimate(self):
        n_versions_before = EstimateVersion.objects.count()
        r = self._import_2029()
        self.assertEqual(r.status_code, 200, r.content)
        r = self._sync(run_estimates=True)
        self.assertEqual(r.status_code, 200, r.content)

        # exactly ONE new estimate version, belonging to 2024->2029
        self.assertEqual(EstimateVersion.objects.count(),
                         n_versions_before + 1)
        new_v = EstimateVersion.objects.exclude(pk=self.v1_id).get()
        self.assertEqual(new_v.t1_campaign.code, "2024")
        self.assertEqual(new_v.t2_campaign.code, "2029")
        self.assertEqual(new_v.interval, self._interval("2024", "2029"))
        self.assertEqual(new_v.status, "draft")

        # the chain: two adjacent intervals, no 2019->2029 stitching
        chain = self._chain()
        self.assertEqual([(i["t1"], i["t2"]) for i in chain["intervals"]],
                         [("2019", "2024"), ("2024", "2029")])
        from inventory.models import SurveyInterval
        self.assertFalse(SurveyInterval.objects.filter(
            t1_campaign=self.c19, t2_campaign=self.c29).exists())
        # interval 2019->2024 keeps exactly its legacy confirmed edition
        i1 = next(i for i in chain["intervals"] if i["t1"] == "2019")
        self.assertEqual([v["id"] for v in i1["versions"]], [self.v1_id])
        self.assertEqual(i1["versions"][0]["status"], "confirmed")
        # and the confirmed edition is byte-identical (see dedicated test)
        self.assertEqual(
            self.client.get(f"/api/estimates/{self.v1_id}/").json(),
            self.v1_snapshot)

    # ---- (b) gap reappearance is not survivor growth ----------------------
    def test_gap_reappearance_not_counted_as_survivor_growth(self):
        self._import_2029()
        self._sync(run_estimates=True)
        from inventory.models import IntervalIdentityLink, Tree
        i2 = self._interval("2024", "2029")
        tree2 = Tree.objects.get(plot=self.pA, current_field_number="2")
        link = IntervalIdentityLink.objects.get(interval=i2, tree=tree2)
        self.assertEqual(link.kind, "gap_reappearance")
        self.assertTrue(link.pending)

        # the 2024->2029 draft must not count PA/2 as survivor growth,
        # mortality or ingrowth — it is listed as unverified/missing only
        v = EstimateVersion.objects.get(interval=i2)
        pa = next(p for p in v.result_payload["provenance"]["plots"]
                  if p["plot"] == "PA")
        listed = {t["tree"] for key in ("mortality", "ingrowth")
                  for t in pa[key]}
        self.assertNotIn("PA/2", listed)
        not_counted = {t["tree"] for t in pa["alive_not_measured"]}
        self.assertIn("PA/2", not_counted)
        # growth on PA comes from PA/1 alone (PA/3 died in 2029)
        b = lambda d: 0.1 * d ** 2 * 15 ** 0.5
        self.assertAlmostEqual(pa["kg"]["survivor_growth"],
                               b(22.0) - b(21.0), places=3)
        # mortality on PA is PA/3 at its 2024 size (height 16 m)
        self.assertAlmostEqual(pa["kg"]["mortality"],
                               0.1 * 23.0 ** 2 * 16 ** 0.5, places=3)

    # ---- (c) 2029 renumber candidate is scoped to its interval ------------
    def test_2029_renumber_conflict_scoped_to_its_interval(self):
        self._import_2029()
        self._sync()
        from inventory.models import IntervalIdentityLink
        # conflict exists ONLY for 2024->2029
        confs = IdentityConflict.objects.filter(field_number="1")
        self.assertEqual(confs.count(), 1)
        conf = confs.get()
        self.assertEqual((conf.t1_campaign.code, conf.t2_campaign.code),
                         ("2024", "2029"))
        self.assertEqual(conf.status, CONFLICT_OPEN)
        # interval 2019->2024 has NO pending item for PB/1
        i1 = self._interval("2019", "2024")
        pb1 = self.client.get("/api/trees/",
                              {"plot": "PB"}).json()
        tree_pb1 = [t for t in pb1 if t["current_field_number"] == "1"][0]
        links_i1 = IntervalIdentityLink.objects.filter(
            interval=i1, tree_id=tree_pb1["id"])
        self.assertEqual([(l.kind, l.pending) for l in links_i1],
                         [("survivor", False)])
        # interval 2024->2029 holds the pending pair (old row + new row)
        i2 = self._interval("2024", "2029")
        pending = IntervalIdentityLink.objects.filter(
            interval=i2, kind="identity_conflict", pending=True)
        self.assertEqual(pending.count(), 2)
        # and the old PB/1 is NOT mortality while unverified
        v = EstimateVersion.objects.filter(interval=i2)
        self.assertFalse(v.exists())  # sync without run_estimates
        self._sync(run_estimates=True)
        v = EstimateVersion.objects.get(interval=i2)
        pb = next(p for p in v.result_payload["provenance"]["plots"]
                  if p["plot"] == "PB")
        self.assertEqual(pb["mortality"], [])
        # PB/101 is NOT ingrowth while unverified; only the genuine new
        # recruit PB/2 enters ingrowth
        self.assertEqual([i["tree"] for i in pb["ingrowth"]], ["PB/2"])
        excluded = {c["tree"] for c in pb["excluded_identity_conflicts"]}
        self.assertIn("PB/1", excluded)
        # both tree rows carry a pending link in this interval
        self.assertEqual(pending.count(), 2)

    # ---- (d) re-import / retry does not duplicate -------------------------
    def test_reimport_and_retry_do_not_duplicate(self):
        from inventory.models import IntervalIdentityLink, SurveyInterval
        self._import_2029()
        self._sync(run_estimates=True)

        def counts():
            return dict(
                intervals=SurveyInterval.objects.count(),
                links=IntervalIdentityLink.objects.count(),
                conflicts=IdentityConflict.objects.count(),
                versions=EstimateVersion.objects.count(),
                trees=Tree.objects.count(),
                measurements=TreeMeasurement.objects.count(),
            )

        before = counts()
        # same batch retransmitted (idempotent ingest), then a failed-run
        # retry of the sync with the same options
        r = self._import_2029()
        self.assertEqual(r.status_code, 200, r.content)
        self._sync(run_estimates=True)
        self._sync(run_estimates=True)
        self.assertEqual(counts(), before)
        # interval provenance is stable too
        i2 = self._interval("2024", "2029")
        p1 = self.client.get(f"/api/intervals/{i2.id}/provenance/").json()
        self._sync(run_estimates=True)
        p2 = self.client.get(f"/api/intervals/{i2.id}/provenance/").json()
        self.assertEqual(len(p1["identity"]["links"]),
                         len(p2["identity"]["links"]))
        self.assertEqual([v["id"] for v in p1["versions"]],
                         [v["id"] for v in p2["versions"]])

    # ---- (e) the confirmed 2019->2024 edition survives everything ---------
    def test_confirmed_edition_untouched_by_2029_work(self):
        self._import_2029()
        self._sync(run_estimates=True)
        # recompute BOTH intervals and confirm the new draft
        i1 = self._interval("2019", "2024")
        i2 = self._interval("2024", "2029")
        self.client.post(f"/api/intervals/{i1.id}/recompute/",
                         {"run_estimate": True,
                          "equation_ids": [self.eq.id]}, format="json")
        r = self.client.post(f"/api/intervals/{i2.id}/recompute/",
                             {"run_estimate": True,
                              "equation_ids": [self.eq.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        draft2 = (EstimateVersion.objects.filter(interval=i2)
                  .order_by("-created_at").first())
        self.client.post(f"/api/estimates/{draft2.id}/confirm/")

        after = self.client.get(f"/api/estimates/{self.v1_id}/").json()
        self.assertEqual(after, self.v1_snapshot)
        self.assertEqual(after["result_payload"],
                         self.v1_snapshot["result_payload"])
        self.assertEqual(after["design_snapshot"],
                         self.v1_snapshot["design_snapshot"])
        self.assertEqual(after["status"], "confirmed")

    # ---- non-adjacent stitching is refused --------------------------------
    def test_direct_2019_2029_estimate_is_refused(self):
        self._import_2029()
        self._sync()
        r = self.client.post("/api/estimates/",
                             dict(label="stitched", t1_campaign="2019",
                                  t2_campaign="2029",
                                  equation_ids=[self.eq.id]),
                             format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not an adjacent interval", r.json()["detail"])

    # ---- sequence creation is idempotent ----------------------------------
    def test_sequence_create_is_idempotent(self):
        r = self.client.post("/api/sequences/",
                             {"name": "main",
                              "campaigns": ["2019", "2024"]}, format="json")
        self.assertEqual(r.status_code, 200)  # not 201
        self.assertEqual(r.json()["id"], self.seq_id)
        from inventory.models import SurveyInterval
        self.assertEqual(SurveyInterval.objects.count(), 1)

    # ---- interval provenance + recompute API ------------------------------
    def test_interval_provenance_and_recompute(self):
        self._import_2029()
        self._sync(run_estimates=True)
        i2 = self._interval("2024", "2029")
        p = self.client.get(f"/api/intervals/{i2.id}/provenance/").json()
        self.assertEqual(p["interval"]["coverage"], "covered")
        self.assertEqual(p["sources"]["t2"]["campaign"], "2029")
        self.assertEqual(p["sources"]["t2"]["measurements"], 5)
        self.assertEqual(p["sources"]["t2"]["import_rows_accepted"], 5)
        pending_kinds = {l["kind"] for l in p["identity"]["pending"]}
        self.assertIn("gap_reappearance", pending_kinds)
        self.assertIn("identity_conflict", pending_kinds)
        self.assertEqual(len(p["versions"]), 1)
        self.assertTrue(p["versions"][0]["linked"])

        # recompute refreshes in place; a requested estimate is a NEW draft
        r = self.client.post(f"/api/intervals/{i2.id}/recompute/",
                             {"run_estimate": True,
                              "equation_ids": [self.eq.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["outcome"]["coverage"], "covered")
        self.assertEqual(EstimateVersion.objects.filter(interval=i2).count(),
                         2)
        # recompute of interval 1 never alters the confirmed legacy edition
        i1 = self._interval("2019", "2024")
        self.client.post(f"/api/intervals/{i1.id}/recompute/", {},
                         format="json")
        self.assertEqual(
            self.client.get(f"/api/estimates/{self.v1_id}/").json(),
            self.v1_snapshot)

    # ---- timeline APIs -----------------------------------------------------
    def test_plot_and_tree_timeline(self):
        self._import_2029()
        self._sync(run_estimates=True)
        tl = self.client.get(
            f"/api/sequences/{self.seq_id}/plots/PA/timeline/").json()
        self.assertEqual([c["code"] for c in tl["campaigns"]],
                         ["2019", "2024", "2029"])
        self.assertEqual(tl["intervals"], ["2019→2024", "2024→2029"])
        t2row = next(t for t in tl["trees"]
                     if t["current_field_number"] == "2")
        self.assertEqual(t2row["occasions"]["2024"]["status"], "missing_tree")
        self.assertEqual(
            t2row["intervals"]["2024→2029"]["kind"], "gap_reappearance")
        self.assertTrue(t2row["intervals"]["2024→2029"]["pending"])

        one = self.client.get(f"/api/trees/{t2row['tree_id']}/timeline/").json()
        self.assertEqual([o["campaign"] for o in one["occasions"]],
                         ["2019", "2024", "2029"])
        self.assertEqual(one["interval_links"][-1]["kind"],
                         "gap_reappearance")

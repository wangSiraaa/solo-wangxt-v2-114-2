"""
Acceptance tests for the MULTI-CAMPAIGN survey sequence (2019 -> 2024 -> 2029).

The station must no longer behave like a two-period report:

  G1. adding 2029 normally creates ONLY the new (2024 -> 2029) interval and
      its estimate edition; the confirmed 2019 -> 2024 edition is untouched;
  G2. a tree missing_tree in 2024 and re-found in 2029 (same row, or no 2024
      row at all but known from 2019) is NEVER stitched into survivor growth
      across the hole — it is a pending gap item on the 2024 -> 2029 link;
  G3. a 2029 near-neighbour relabel only generates the pending identity item
      on the 2024 -> 2029 interval, never on 2019 -> 2024;
  G4. retransmission / failed retry of the same 2029 upload is idempotent —
      no duplicated batches, conflicts or interval links;
  G5. a legacy confirmed 2019 -> 2024 edition still returns its frozen
      numbers and snapshot (interval_id is null).
"""
from rest_framework.test import APIClient

from django.test import TestCase

from inventory.models import (
    AllometricEquation,
    Campaign,
    EstimateVersion,
    IdentityConflict,
    ImportBatch,
    IntervalLink,
    LINK_INGROWTH,
    LINK_MORTALITY,
    LINK_PENDING_GAP,
    LINK_PENDING_RENUMBER,
    LINK_SAME_NUMBER,
    MeasurementImportRow,
    Plot,
    Species,
    Stratum,
    SurveyInterval,
    SurveySequence,
    Tree,
    TreeMeasurement,
)
from inventory.services.ingest import import_campaign_rows
from inventory.services.sequences import (
    add_campaigns,
    create_sequence,
    interval_provenance,
    refresh_interval,
)


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM, AN, DE, MI = ("alive_measured", "alive_not_measured",
                 "dead", "missing_tree")


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.c19 = Campaign.objects.create(code="2019", measured_on="2019-07-01")
        self.c24 = Campaign.objects.create(code="2024", measured_on="2024-07-01")
        self.c29 = Campaign.objects.create(code="2029", measured_on="2029-07-01")
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)

    def row(self, num, status, x=5, y=5, dbh=20.0, h=15.0, sp="OAK"):
        d = dict(plot="P1", field_number=num, species=sp, x_m=x, y_m=y,
                 status=status)
        if dbh is not None:
            d.update(dbh_raw=dbh, dbh_unit="cm")
        if h is not None:
            d.update(height_raw=h, height_unit="m")
        return d

    def make_sequence(self, codes):
        seq, _ = create_sequence("MAIN", "Main census chain", codes)
        for iv in seq.intervals.all():
            refresh_interval(iv)
        return seq

    def iv(self, t1, t2):
        return SurveyInterval.objects.get(
            t1_campaign__code=t1, t2_campaign__code=t2)

    def run_strict_estimate(self, iv, label=None):
        r = self.client.post(
            f"/api/intervals/{iv.id}/estimates/",
            {"label": label or f"Draft {iv.code}",
             "equation_ids": [self.eq.id], "fpc": False},
            format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()


class NormalThirdCensusTests(_Base):
    def test_adding_2029_only_adds_the_second_interval_estimate(self):
        # 2019 / 2024: one survivor (growth) and one mortality.
        import_campaign_rows(self.c19, [
            self.row("001", AM, dbh=20.0),
            self.row("006", AM, x=11, y=11, dbh=22.0, h=16.0),
        ], 0.01)
        import_campaign_rows(self.c24, [
            self.row("001", AM, dbh=21.0),
            self.row("006", DE, x=11, y=11, dbh=None, h=None),
        ], 0.01)

        seq = self.make_sequence(["2019", "2024"])
        iv0 = self.iv("2019", "2024")
        v0 = self.run_strict_estimate(iv0, label="v0-2019-2024")
        cr = self.client.post(f"/api/estimates/{v0['id']}/confirm/")
        self.assertEqual(cr.status_code, 200, cr.content)
        g0 = v0["result_payload"]["components"]["survivor_growth"]
        m0 = v0["result_payload"]["components"]["mortality"]

        # ---- third census arrives ----
        import_campaign_rows(self.c29, [
            self.row("001", AM, dbh=22.0),
        ], 0.01)
        out = add_campaigns(seq, ["2029"])
        self.assertEqual(out["added"], ["2029"])
        self.assertEqual(out["new_intervals"], ["2024->2029"])
        iv1 = self.iv("2024", "2029")

        # exactly two adjacent links; 2019 and 2029 are not directly paired
        self.assertEqual(
            list(seq.intervals.order_by("ordinal").values_list(
                "t1_campaign__code", "t2_campaign__code")),
            [("2019", "2024"), ("2024", "2029")])
        # iv0 was not rebuilt into a new estimate edition
        self.assertEqual(iv0.estimate_versions.count(), 1)
        v1 = self.run_strict_estimate(iv1, label="v1-2024-2029")
        self.assertEqual(v1["interval_id"], iv1.id)
        self.assertEqual(iv1.estimate_versions.count(), 1)
        self.assertEqual(iv0.estimate_versions.count(), 1)
        # iv1 growth is only 2024 -> 2029 (21 -> 22), iv0 is its own (20->21)
        g1 = v1["result_payload"]["components"]["survivor_growth"]
        self.assertNotAlmostEqual(
            g1["total_kg"], g0["total_kg"], places=6)
        # mortality belongs to iv0 only: 006 had no 2024 survivor, nothing
        # for it in the 2024 -> 2029 link.
        self.assertEqual(
            v1["result_payload"]["components"]["mortality"]["total_kg"], 0.0)

        # frozen edition still serves the original numbers and snapshot
        again = self.client.get(f"/api/estimates/{v0['id']}/").json()
        self.assertEqual(again["status"], "confirmed")
        self.assertEqual(again["interval_id"], iv0.id)
        self.assertEqual(
            again["result_payload"]["components"]["survivor_growth"], g0)
        self.assertEqual(
            again["result_payload"]["components"]["mortality"], m0)


class GapChainTests(_Base):
    def _build_gap_scenario(self):
        # G1: same tracked row, missing_tree at 2024, alive again at 2029.
        import_campaign_rows(self.c19, [self.row("g1", AM, x=5, y=5,
                                                 dbh=20.0)], 0.01)
        import_campaign_rows(self.c24, [self.row("g1", MI, x=5, y=5,
                                                 dbh=None, h=None)], 0.01)
        import_campaign_rows(self.c29, [self.row("g1", AM, x=5, y=5,
                                                 dbh=23.0)], 0.01)
        # G2: alive 2019, NO row at 2024 at all, re-found 2029 (same tag,
        # same spot -> same tracked row, hole in the middle).
        import_campaign_rows(self.c19, [self.row("g2", AM, x=8, y=8,
                                                 dbh=18.0)], 0.01)
        import_campaign_rows(self.c29, [self.row("g2", AM, x=8, y=8,
                                                 dbh=20.0)], 0.01)
        return self.make_sequence(["2019", "2024", "2029"])

    def test_gap_trees_are_pending_and_never_survivor_growth(self):
        self._build_gap_scenario()
        iv0 = self.iv("2019", "2024")
        iv1 = self.iv("2024", "2029")

        # the 2024 -> 2029 link carries two pending gap items
        gaps = iv1.links.filter(kind=LINK_PENDING_GAP)
        self.assertEqual(gaps.count(), 2)
        self.assertTrue(all(g.excluded_from_components for g in gaps))
        tags = {g.t2_field_number for g in gaps}
        self.assertEqual(tags, {"g1", "g2"})

        # 2019 -> 2024 has NO gap item for these trees
        self.assertEqual(iv0.links.filter(kind=LINK_PENDING_GAP).count(), 0)
        # g1 was "not located" at 2024, not mortality
        prov0 = interval_provenance(iv0)
        self.assertIn("g1", [
            l["t1_field_number"] or l["t2_field_number"]
            for l in prov0["links"] if l["kind"] == "not_located_t2"])

        # STRICT estimate for 2024 -> 2029: no growth/ingrowth/mortality may
        # be booked across the hole.
        v1 = self.run_strict_estimate(iv1)
        comps = v1["result_payload"]["components"]
        self.assertEqual(comps["survivor_growth"]["total_kg"], 0.0)
        self.assertEqual(comps["mortality"]["total_kg"], 0.0)
        self.assertEqual(comps["ingrowth"]["total_kg"], 0.0)
        pending = v1["result_payload"]["provenance"][
            "pending_gap_reappearances"]
        self.assertEqual({p["field_number"] for p in pending}, {"g1", "g2"})

        # timeline must show a BROKEN edge at 2024 -> 2029, never a direct
        # 2019 -> 2029 survivor line.
        t = self.client.get(
            f"/api/timeline/?sequence=MAIN&plot=P1").json()
        g1_ind = next(i for i in t["individuals"]
                      if i["current_field_number"] == "g1")
        # g1 WAS sought at 2024 and recorded missing_tree — the node exists
        # but the 2024 -> 2029 chain edge is a hole, not survival.
        self.assertEqual(g1_ind["nodes"][1]["status"], "missing_tree")
        edge19_24 = next(e for e in g1_ind["edges"]
                         if e["interval"] == "2019->2024")
        self.assertEqual(edge19_24["kind"], "not_located_t2")
        edge24_29 = next(e for e in g1_ind["edges"]
                         if e["interval"] == "2024->2029")
        self.assertTrue(edge24_29["pending"])
        self.assertTrue(edge24_29["gap"])
        self.assertFalse(g1_ind["chain_complete"])

        # g2 has NO 2024 node at all, and the same broken edge.
        g2_ind = next(i for i in t["individuals"]
                      if i["current_field_number"] == "g2")
        self.assertIsNone(g2_ind["nodes"][1])
        g2_edge = next(e for e in g2_ind["edges"]
                       if e["interval"] == "2024->2029")
        self.assertTrue(g2_edge["gap"])

    def test_human_checking_the_gap_still_does_not_count_growth(self):
        self._build_gap_scenario()
        iv1 = self.iv("2024", "2029")
        conflict = IdentityConflict.objects.get(
            t1_campaign=self.c24, t2_campaign=self.c29,
            t2_measurement__tree__current_field_number="g1")
        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"status": "renumber", "note": "tag confirmed after search"})
        self.assertEqual(r.status_code, 200, r.content)
        iv1.refresh_from_db()
        link = iv1.links.get(t2_field_number="g1")
        self.assertEqual(link.kind, "gap_reappearance_verified")
        self.assertTrue(link.excluded_from_components)
        v1 = self.run_strict_estimate(iv1)
        self.assertEqual(
            v1["result_payload"]["components"]["survivor_growth"]["total_kg"],
            0.0)


class NearNeighbourRelabelTests(_Base):
    def test_2029_relabel_only_flags_2024_2029_interval(self):
        # 117 alive in 2024; in 2029 the old tag disappears and a NEW tag
        # 118 shows up ~0.4 m away (suspected unrecorded tag replacement).
        import_campaign_rows(self.c24, [
            self.row("117", AM, x=10, y=10, dbh=19.0),
        ], 0.01)
        import_campaign_rows(self.c29, [
            self.row("118", AM, x=10.4, y=10.2, dbh=19.9),
        ], 0.01)
        seq = self.make_sequence(["2024", "2029"])
        iv1 = self.iv("2024", "2029")

        pend = iv1.links.filter(kind=LINK_PENDING_RENUMBER)
        self.assertEqual(pend.count(), 1)
        self.assertEqual(pend.get().t2_field_number, "118")
        self.assertTrue(pend.get().excluded_from_components)
        # conflict exists only on 2024 -> 2029
        self.assertEqual(IdentityConflict.objects.filter(
            t1_campaign=self.c24, t2_campaign=self.c29).count(), 1)
        self.assertEqual(IdentityConflict.objects.filter(
            t1_campaign=self.c19).count(), 0)

        v1 = self.run_strict_estimate(iv1)
        comps = v1["result_payload"]["components"]
        # no automatic merge: not survivor growth and not ingrowth
        self.assertEqual(comps["survivor_growth"]["total_kg"], 0.0)
        self.assertEqual(comps["ingrowth"]["total_kg"], 0.0)
        p1p = next(p for p in v1["result_payload"]["provenance"]["plots"]
                   if p["plot"] == "P1")
        excluded = {x["t2_tree"] or x["tree"]
                    for x in p1p["excluded_identity_conflicts"]}
        self.assertIn("P1/118", excluded)


class IdempotentUploadTests(_Base):
    def _rows(self):
        return [
            self.row("001", AM, x=5, y=5, dbh=20.0),
            self.row("118", AM, x=10.4, y=10.2, dbh=12.0),
            self.row("117", AM, x=10, y=10, dbh=12.1),
        ]

    def test_retransmit_and_retry_do_not_duplicate(self):
        # 2024 anchor so a sequence exists
        import_campaign_rows(self.c24, [
            self.row("001", AM, x=5, y=5, dbh=19.0),
            self.row("117", AM, x=10, y=10, dbh=11.0),
        ], 0.01)
        seq = self.make_sequence(["2024", "2029"])

        body = {"campaign": "2029", "client_batch_id": "B-2029-001",
                "rows": self._rows()}
        r1 = self.client.post("/api/imports/", body, format="json")
        self.assertIn(r1.status_code, (200, 207), r1.content)
        n_meas = TreeMeasurement.objects.filter(campaign=self.c29).count()
        n_audit = MeasurementImportRow.objects.filter(
            campaign=self.c29).count()
        n_batch = ImportBatch.objects.count()
        iv1 = self.iv("2024", "2029")
        n_links = iv1.links.count()
        n_conf = IdentityConflict.objects.filter(
            t1_campaign=self.c24, t2_campaign=self.c29).count()

        # exact retransmission -> stored replay, no duplicates
        r2 = self.client.post("/api/imports/", body, format="json")
        self.assertTrue(r2.json().get("idempotent_replay"))
        self.assertEqual(r2.json()["batch_id"], r1.json()["batch_id"])
        # failed retry with NO client id but the SAME payload also replays
        body3 = {"campaign": "2029", "rows": self._rows()}
        r3 = self.client.post("/api/imports/", body3, format="json")
        self.assertTrue(r3.json()["idempotent_replay"])

        self.assertEqual(
            TreeMeasurement.objects.filter(campaign=self.c29).count(),
            n_meas)
        self.assertEqual(
            MeasurementImportRow.objects.filter(campaign=self.c29).count(),
            n_audit)
        self.assertEqual(ImportBatch.objects.count(), n_batch)
        iv1.refresh_from_db()
        self.assertEqual(iv1.links.count(), n_links)
        self.assertEqual(IdentityConflict.objects.filter(
            t1_campaign=self.c24, t2_campaign=self.c29).count(), n_conf)

        # same batch id with DIFFERENT rows is a 409, not an overwrite
        bad = dict(body)
        bad["rows"] = [self.row("999", AM, x=5, y=5, dbh=20.0)]
        r4 = self.client.post("/api/imports/", bad, format="json")
        self.assertEqual(r4.status_code, 409)


class LegacyConfirmedEditionTests(_Base):
    def test_legacy_confirmed_2019_2024_snapshot_frozen(self):
        import_campaign_rows(self.c19, [self.row("001", AM, dbh=20.0)], 0.01)
        import_campaign_rows(self.c24, [self.row("001", AM, dbh=21.0)], 0.01)
        # legacy draft endpoint (no interval attached)
        r = self.client.post("/api/estimates/",
                             {"label": "legacy", "t1_campaign": "2019",
                              "t2_campaign": "2024",
                              "equation_ids": [self.eq.id], "fpc": False},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        self.assertIsNone(r.json()["interval_id"])
        frozen_payload = r.json()["result_payload"]
        self.client.post(f"/api/estimates/{vid}/confirm/")

        # now build the full chain and bring in 2029
        import_campaign_rows(self.c29, [self.row("001", AM, dbh=22.0)], 0.01)
        seq = self.make_sequence(["2019", "2024", "2029"])

        old = self.client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(old["status"], "confirmed")
        self.assertIsNone(old["interval_id"])
        self.assertEqual(old["result_payload"], frozen_payload)
        self.assertEqual(
            old["design_snapshot"]["t1_code"], "2019")
        # legacy design stays non-strict (flag recorded as False)
        self.assertIs(
            old["design_snapshot"].get("strict_gap_chain"), False)

    def test_sequence_create_complete_and_provenance_api(self):
        import_campaign_rows(self.c19, [self.row("001", AM)], 0.01)
        import_campaign_rows(self.c24, [self.row("001", AM, dbh=21.0)], 0.01)
        # create with just the first two campaigns ...
        r = self.client.post("/api/sequences/",
                             {"code": "MAIN", "name": "chain",
                              "campaigns": ["2019", "2024"]},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        seq_id = r.json()["id"]
        # ... then complete it with 2029
        import_campaign_rows(self.c29, [self.row("001", AM, dbh=22.0)], 0.01)
        r2 = self.client.post(
            f"/api/sequences/{seq_id}/add_campaigns/",
            {"campaigns": ["2029"]}, format="json")
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertEqual(r2.json()["added"], ["2029"])

        ivs = self.client.get(
            "/api/intervals/?sequence=MAIN").json()
        self.assertEqual([i["t1_code"] for i in ivs], ["2019", "2024"])
        self.assertEqual([i["t2_code"] for i in ivs], ["2024", "2029"])
        self.assertTrue(all(i["status"] == "built" for i in ivs))

        iv1 = self.iv("2024", "2029")
        prov = self.client.get(
            f"/api/intervals/{iv1.id}/provenance/").json()
        self.assertEqual(prov["interval"], "2024->2029")
        self.assertEqual(prov["coverage"]["n_t2_measurements"], 1)
        self.assertTrue(prov["coverage"]["fingerprint"])
        self.assertEqual(len(prov["links"]), 1)

        # refresh is idempotent
        rr = self.client.post(f"/api/intervals/{iv1.id}/refresh/")
        self.assertEqual(rr.status_code, 200, rr.content)
        fp = rr.json()["fingerprint"]
        rr2 = self.client.post(f"/api/intervals/{iv1.id}/refresh/")
        self.assertEqual(rr2.json()["fingerprint"], fp)
        self.assertEqual(iv1.links.count(), 1)

    def test_tree_timeline_endpoint(self):
        import_campaign_rows(self.c19, [self.row("001", AM)], 0.01)
        import_campaign_rows(self.c24, [self.row("001", AM, dbh=21.0)], 0.01)
        import_campaign_rows(self.c29, [self.row("001", AM, dbh=22.0)], 0.01)
        self.make_sequence(["2019", "2024", "2029"])
        tree = Tree.objects.get(current_field_number="001")
        t = self.client.get(
            f"/api/timeline/?sequence=MAIN&tree={tree.id}").json()
        self.assertEqual([n["campaign"] for n in t["individual"]["nodes"]],
                         ["2019", "2024", "2029"])
        kinds = [e["kind"] for e in t["individual"]["edges"]]
        self.assertEqual(kinds, [LINK_SAME_NUMBER, LINK_SAME_NUMBER])
        self.assertTrue(t["individual"]["chain_complete"])

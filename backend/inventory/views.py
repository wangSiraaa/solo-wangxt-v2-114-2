"""
REST API:

GET  /plots/                              plot positions + boundaries
GET  /trees/?campaign=CODE                individuals and remeasurement status
GET  /trees/{id}/timeline/                one individual across the whole chain
GET  /conflicts/                          same-number position contradictions
POST /conflicts/{id}/resolve/             human verification only
POST /imports/                            ingest a campaign's field rows
POST /estimates/                          run (or rerun) a DRAFT estimate
POST /estimates/{id}/confirm/             freeze forever; locks equations
GET  /estimates/{id}/                     frozen result with provenance

Multi-period survey sequences (adjacent-interval chain):

GET  /sequences/                          chains with per-interval summaries
POST /sequences/                          create (idempotent by name)
POST /sequences/{id}/sync/                backfill campaigns + intervals,
                                          refresh coverage/links, optionally
                                          draft estimates for new intervals
GET  /sequences/{id}/plots/{code}/timeline/  per-individual matrix for a plot
GET  /intervals/{id}/                     one interval
GET  /intervals/{id}/provenance/          interval sources, identity links,
                                          conflicts and estimate editions
POST /intervals/{id}/recompute/           refresh one interval (never touches
                                          confirmed editions or other intervals)
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    CONFLICT_RENUMBER,
    EstimateVersion,
    IdentityConflict,
    Plot,
    Species,
    Stratum,
    SurveyInterval,
    SurveySequence,
    Tree,
    TreeMeasurement,
    VERSION_CONFIRMED,
)
from inventory.serializers import (
    CampaignSerializer,
    ConflictResolveSerializer,
    ConflictSerializer,
    EquationSerializer,
    EstimateVersionSerializer,
    MeasurementImportSerializer,
    MeasurementSerializer,
    PlotSerializer,
    SpeciesSerializer,
    StratumSerializer,
    SurveyIntervalSerializer,
    TreeSerializer,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)
from inventory.services.ingest import import_campaign_rows
from inventory.services.sequence import (
    interval_provenance,
    plot_timeline,
    recompute_interval,
    refresh_interval,
    sequence_chain,
    sync_sequence,
    tree_timeline,
)


class StratumViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Stratum.objects.all()
    serializer_class = StratumSerializer


class SpeciesViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Species.objects.all()
    serializer_class = SpeciesSerializer


class CampaignViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Campaign.objects.all()
    serializer_class = CampaignSerializer


class EquationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AllometricEquation.objects.prefetch_related("species").all()
    serializer_class = EquationSerializer


class PlotViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Plot.objects.select_related("stratum").all()
    serializer_class = PlotSerializer


class TreeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TreeSerializer

    def get_queryset(self):
        qs = Tree.objects.select_related("plot", "species", "superseded_tree")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(measurements__campaign__code=campaign).distinct()
        plot = self.request.query_params.get("plot")
        if plot:
            qs = qs.filter(plot__code=plot)
        return qs

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """Chronological trace of one individual across the whole chain."""
        return Response(tree_timeline(self.get_object()))


class MeasurementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MeasurementSerializer

    def get_queryset(self):
        qs = TreeMeasurement.objects.select_related("tree", "tree__plot",
                                                    "campaign")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        return qs


class ConflictViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ConflictSerializer

    def get_queryset(self):
        qs = IdentityConflict.objects.select_related("plot")
        state = self.request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        interval = self.request.query_params.get("interval")
        if interval:
            iv = SurveyInterval.objects.filter(pk=interval).first()
            if iv:
                qs = qs.filter(t1_campaign=iv.t1_campaign,
                               t2_campaign=iv.t2_campaign)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Human-in-the-loop resolution. Nothing here is automatic."""
        conflict = self.get_object()
        ser = ConflictResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if conflict.status != CONFLICT_OPEN:
            return Response(
                {"detail": f"conflict already resolved as {conflict.status}; "
                           "verification cannot be undone here."},
                status=status.HTTP_409_CONFLICT,
            )
        decision = ser.validated_data["status"]
        with transaction.atomic():
            if decision == CONFLICT_RENUMBER:
                # same individual: t2's tree row becomes a successor of t1's
                t2_tree = conflict.t2_measurement.tree
                t1_tree = conflict.t1_measurement.tree
                if t2_tree != t1_tree:
                    t2_tree.superseded_tree = t1_tree
                    t2_tree.current_field_number = (
                        conflict.t2_measurement.field_number_seen
                    )
                    t2_tree.save(update_fields=["superseded_tree",
                                               "current_field_number"])
            # distinct: do nothing — t1 and t2 rows stay separate and enter
            # mortality / ingrowth candidates respectively.
            conflict.status = decision
            conflict.resolution_note = ser.validated_data.get("note", "")
            conflict.resolved_at = timezone.now()
            conflict.save()
            # the resolution changes identity determinations in exactly one
            # interval — refresh just that one
            for iv in SurveyInterval.objects.filter(
                    t1_campaign=conflict.t1_campaign,
                    t2_campaign=conflict.t2_campaign):
                refresh_interval(iv, scan=False)
        return Response(ConflictSerializer(conflict).data)


class ImportViewSet(viewsets.ViewSet):
    def create(self, request):
        ser = MeasurementImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        campaign = Campaign.objects.filter(
            code=ser.validated_data["campaign"]
        ).first()
        if campaign is None:
            return Response({"detail": "unknown campaign"},
                            status=status.HTTP_404_NOT_FOUND)
        result = import_campaign_rows(
            campaign, ser.validated_data["rows"],
            area_tolerance=settings.PLOT_AREA_TOLERANCE,
        )

        # Re-scan identity contradictions against ADJACENT campaigns only.
        # Identity relations live inside one interval: the 2029 batch is
        # checked against 2024, never stitched straight back to 2019.
        result["conflicts"] = []
        prev_c = (Campaign.objects
                  .filter(measured_on__lt=campaign.measured_on)
                  .order_by("-measured_on").first())
        next_c = (Campaign.objects
                  .filter(measured_on__gt=campaign.measured_on)
                  .order_by("measured_on").first())
        for t1, t2 in ((prev_c, campaign), (campaign, next_c)):
            if t1 is None or t2 is None:
                continue
            if not TreeMeasurement.objects.filter(campaign=t1).exists():
                continue
            if not TreeMeasurement.objects.filter(campaign=t2).exists():
                continue
            result["conflicts"] += scan_conflicts(t1, t2)

        # refresh any intervals this campaign belongs to (coverage + links)
        result["refreshed_intervals"] = []
        affected = (SurveyInterval.objects.filter(t1_campaign=campaign)
                    | SurveyInterval.objects.filter(t2_campaign=campaign))
        for iv in affected:
            out = refresh_interval(iv, scan=False)
            result["refreshed_intervals"].append(
                f"{iv.t1_campaign.code}→{iv.t2_campaign.code} "
                f"[{out['coverage']}]")

        return Response(result,
                        status=status.HTTP_207_MULTI_STATUS if result["rejected"]
                        else status.HTTP_200_OK)


class EstimateViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = EstimateVersion.objects.all().order_by("-created_at")
        return Response(EstimateVersionSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(
            EstimateVersionSerializer(_get_version(pk)).data
        )

    def create(self, request):
        """
        Body: either {"interval_id": N, ...} for an adjacent-chain interval,
        or {"t1_campaign": CODE, "t2_campaign": CODE, ...} (legacy two-period
        form, allowed only when no sequence forbids the pairing).
        Plus: "label", "equation_ids": [...], "fpc": true.
        Creates (or recomputes) a DRAFT. Confirmation is a separate action.
        """
        label = request.data.get("label", "draft estimate")
        interval = None
        if request.data.get("interval_id"):
            interval = SurveyInterval.objects.filter(
                pk=request.data["interval_id"]).first()
            if interval is None:
                return Response({"detail": "unknown interval_id"},
                                status=status.HTTP_404_NOT_FOUND)
            t1, t2 = interval.t1_campaign, interval.t2_campaign
        else:
            t1 = Campaign.objects.filter(
                code=request.data.get("t1_campaign")).first()
            t2 = Campaign.objects.filter(
                code=request.data.get("t2_campaign")).first()
            if not t1 or not t2 or t1.measured_on >= t2.measured_on:
                return Response(
                    {"detail": "need t1 earlier than t2 campaign codes"},
                    status=status.HTTP_400_BAD_REQUEST)
            violation = _non_adjacent_pair_violation(t1, t2)
            if violation:
                return Response({"detail": violation},
                                status=status.HTTP_400_BAD_REQUEST)

        eq_ids = request.data.get("equation_ids", [])
        equations_qs = AllometricEquation.objects.filter(
            id__in=eq_ids
        ).prefetch_related("species")
        if equations_qs.count() != len(eq_ids) or not eq_ids:
            return Response({"detail": "equation_ids invalid/empty"},
                            status=status.HTTP_400_BAD_REQUEST)

        version = _run_estimate_for(
            t1, t2, equations_qs, label=label,
            fpc=bool(request.data.get("fpc", True)), interval=interval)
        return Response(EstimateVersionSerializer(version).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Freeze the edition forever and lock its equations."""
        version = _get_version(pk)
        if version.status == VERSION_CONFIRMED:
            return Response({"detail": "already confirmed"},
                            status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            # re-verify checksum: equations must not have drifted since run
            eqs = version.equations.all().prefetch_related("species")
            equations = {}
            for e in eqs:
                for sp in e.species.all():
                    equations[sp.code] = {
                        "code": e.code, "version": e.version,
                        "a": e.a, "b": e.b, "c": e.c,
                        "dbh_min_cm": e.dbh_min_cm,
                        "dbh_max_cm": e.dbh_max_cm,
                        "height_required": e.height_required,
                        "residual_sigma": e.residual_sigma,
                        "citation": e.citation,
                    }
            current = equation_checksum(equations)
            if current != version.equation_checksum:
                return Response(
                    {"detail": "equations changed since the run; create a "
                               "new version rather than confirming stale "
                               "numbers."},
                    status=status.HTTP_409_CONFLICT)
            version.status = VERSION_CONFIRMED
            version.confirmed_at = timezone.now()
            version.save()
            # Lock the equations: a confirmed edition's equation is frozen
            # and a new coefficient set must be issued as a new equation row.
            from inventory.models import EQUATION_CONFIRMED
            eqs.update(status=EQUATION_CONFIRMED)
        return Response(EstimateVersionSerializer(version).data)


def _non_adjacent_pair_violation(t1, t2):
    """
    Once a sequence chains campaigns together, estimates may only be built
    for ADJACENT pairs of that chain. A direct first-to-last estimate
    (e.g. 2019 -> 2029) would stitch across a gap and is refused.
    """
    for seq in SurveySequence.objects.prefetch_related("memberships"):
        camps = seq.ordered_campaigns()
        codes = [c.code for c in camps]
        if t1.code not in codes or t2.code not in codes:
            continue
        i, j = codes.index(t1.code), codes.index(t2.code)
        if j - i != 1:
            return (f"{t1.code}→{t2.code} is not an adjacent interval of "
                    f"sequence '{seq.name}' ({' → '.join(codes)}). Estimate "
                    "each adjacent interval separately; never stitch "
                    "non-adjacent campaigns into continuous survival.")
    return None


def _run_estimate_for(t1, t2, equations_qs, label, fpc=True, interval=None):
    """Shared estimate runner used by the API and by interval sync."""
    table_t1, table_t2, equations, plots, strata = (
        build_measurement_table(t1, t2, equations_qs)
    )
    uncovered = sorted({
        r["species"] for r in table_t1 + table_t2
        if r["species"] not in equations
    })
    renumber, distinct = resolved_identity_pairs(t1, t2)

    interval_years = round(
        (t2.measured_on - t1.measured_on).days / 365.25, 3)
    design = {
        "t1_code": t1.code, "t2_code": t2.code,
        "interval_years": interval_years,
        "dbh_sd_cm": settings.DBH_MEASUREMENT_SD_CM,
        "height_sd_m": settings.HEIGHT_MEASUREMENT_SD_M,
        "zero_tol_cm": settings.ZERO_GROWTH_TOL_CM,
        "recruitment_cm": settings.RECRUITMENT_DBH_CM,
        "fpc": bool(fpc),
        "crs_epsg": settings.SURVEY_CRS_EPSG,
    }
    result = estimate(table_t1, table_t2, equations, plots, strata,
                      design,
                      resolved_renumber_pairs=renumber,
                      resolved_distinct_pairs=distinct)
    result["species_without_equation"] = uncovered
    checksum = equation_checksum(equations)

    snap_strata = {code: {**s, "plot_codes": list(s["plot_codes"])}
                   for code, s in strata.items()}
    design_snapshot = {**design,
                       "strata": snap_strata,
                       "equation_ids": sorted(
                           equations_qs.values_list("id", flat=True)),
                       "equation_codes": {sp: e["code"] + "@" + e["version"]
                                          for sp, e in equations.items()},
                       "area_tolerance": settings.PLOT_AREA_TOLERANCE}
    if interval is not None:
        design_snapshot["interval_id"] = interval.id
        design_snapshot["sequence"] = interval.sequence.name

    version = EstimateVersion.objects.create(
        label=label, t1_campaign=t1, t2_campaign=t2, interval=interval,
        design_snapshot=design_snapshot,
        result_payload=result, equation_checksum=checksum,
    )
    version.equations.set(equations_qs)
    return version


class SequenceViewSet(viewsets.ViewSet):
    """Survey sequences: the adjacent-interval chain of campaigns."""

    def list(self, request):
        return Response([sequence_chain(s)
                         for s in SurveySequence.objects.all()])

    def retrieve(self, request, pk=None):
        from django.shortcuts import get_object_or_404
        seq = get_object_or_404(SurveySequence, pk=pk)
        return Response(sequence_chain(seq))

    def create(self, request):
        """
        Body: {"name": ..., "campaigns": [codes in chain order] (optional)}.
        Idempotent by name: an existing sequence is returned, not duplicated.
        """
        from inventory.services.sequence import get_or_create_sequence
        name = request.data.get("name")
        if not name:
            return Response({"detail": "name required"},
                            status=status.HTTP_400_BAD_REQUEST)
        codes = request.data.get("campaigns") or None
        if codes:
            missing = [c for c in codes
                       if not Campaign.objects.filter(code=c).exists()]
            if missing:
                return Response({"detail": f"unknown campaigns {missing}"},
                                status=status.HTTP_400_BAD_REQUEST)
        seq, created = get_or_create_sequence(name, codes)
        return Response(sequence_chain(seq),
                        status=status.HTTP_201_CREATED if created
                        else status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def sync(self, request, pk=None):
        """
        补齐调查序列: pull in new campaigns, create missing adjacent
        intervals, refresh coverage/identity links, and optionally draft
        estimates for intervals that have none. Safe to retry — nothing
        is duplicated and confirmed editions are never touched.

        Body (all optional): {"campaigns": [codes], "run_estimates": bool,
                              "equation_ids": [...], "fpc": bool}
        """
        from django.shortcuts import get_object_or_404
        seq = get_object_or_404(SurveySequence, pk=pk)
        codes = request.data.get("campaigns") or None
        if codes:
            missing = [c for c in codes
                       if not Campaign.objects.filter(code=c).exists()]
            if missing:
                return Response({"detail": f"unknown campaigns {missing}"},
                                status=status.HTTP_400_BAD_REQUEST)
        summary = sync_sequence(
            seq, extra_campaign_codes=codes,
            run_estimates=bool(request.data.get("run_estimates", False)),
            equation_ids=request.data.get("equation_ids") or None,
            fpc=bool(request.data.get("fpc", True)),
        )
        return Response({"summary": summary, "sequence": sequence_chain(seq)})

    @action(detail=True, methods=["get"],
            url_path="plots/(?P<plot_code>[^/.]+)/timeline")
    def plot_timeline(self, request, plot_code=None, pk=None):
        """Per-individual chain matrix for one plot (时间线界面数据源)."""
        from django.shortcuts import get_object_or_404
        seq = get_object_or_404(SurveySequence, pk=pk)
        return Response(plot_timeline(seq, plot_code))


class IntervalViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SurveyIntervalSerializer

    def get_queryset(self):
        return SurveyInterval.objects.select_related(
            "t1_campaign", "t2_campaign", "sequence").all()

    @action(detail=True, methods=["get"])
    def provenance(self, request, pk=None):
        """区间来源: imports, identity determinations, conflicts, editions."""
        return Response(interval_provenance(self.get_object()))

    @action(detail=True, methods=["post"])
    def recompute(self, request, pk=None):
        """
        重算区间: refresh this interval's coverage, identity links and
        conflicts; optionally add a NEW draft edition
        ({"run_estimate": true, "equation_ids": [...], "label": ...}).
        Confirmed editions and all other intervals are left untouched.
        """
        interval = self.get_object()
        try:
            outcome = recompute_interval(
                interval,
                run_estimate=bool(request.data.get("run_estimate", False)),
                equation_ids=request.data.get("equation_ids") or None,
                fpc=bool(request.data.get("fpc", True)),
                label=request.data.get("label") or None,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"outcome": outcome,
                         "provenance": interval_provenance(interval)})


def _get_version(pk):
    from django.shortcuts import get_object_or_404
    return get_object_or_404(
        EstimateVersion.objects.prefetch_related("equations"), pk=pk)

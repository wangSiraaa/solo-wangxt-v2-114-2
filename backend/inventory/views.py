"""
REST API:

Two-campaign (legacy, unchanged)
================================
GET  /plots/                        plot positions + boundaries
GET  /trees/?campaign=CODE          individuals and remeasurement status
POST /imports/                      ingest rows (idempotent via batch id)
POST /estimates/                    run (or rerun) a DRAFT estimate
POST /estimates/{id}/confirm/       freeze forever; locks equations
GET  /estimates/{id}/               frozen result with provenance

Multi-campaign chains
=====================
POST /sequences/                    create a survey sequence + adjacent links
GET  /sequences/                    list sequences
GET  /sequences/{id}/               sequence with campaigns + intervals
POST /sequences/{id}/add_campaigns/ create/complete a chain (only new links)
GET  /intervals/                    every chain link (filter ?sequence=)
GET  /intervals/{id}/               one link: coverage, links, editions
POST /intervals/{id}/refresh/       (re)build coverage + identity links
GET  /intervals/{id}/provenance/    full source listing of the link
POST /intervals/{id}/estimates/     STRICT gap-chain draft edition
GET  /timeline/?sequence=&plot=     per-plot timeline
GET  /timeline/?sequence=&tree=     per-individual timeline
GET  /import-batches/               idempotency envelopes (audit)

Identity
========
GET  /conflicts/                    per-interval identity items
POST /conflicts/{id}/resolve/       human verification only
"""
import hashlib
import json

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    EstimateVersion,
    IdentityConflict,
    ImportBatch,
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
    ImportBatchSerializer,
    IntervalEstimateRunSerializer,
    IntervalSerializer,
    MeasurementImportSerializer,
    MeasurementSerializer,
    PlotSerializer,
    SequenceAddCampaignsSerializer,
    SequenceCreateSerializer,
    SequenceSerializer,
    SpeciesSerializer,
    StratumSerializer,
    TreeSerializer,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.ingest import import_campaign_rows
from inventory.services.sequences import (
    add_campaigns,
    create_sequence,
    interval_provenance,
    refresh_adjacent_intervals,
    refresh_interval,
    run_interval_estimate,
)
from inventory.services.timeline import plot_timeline, tree_timeline
from inventory.views_estimates import (
    run_legacy_estimate,
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
        seq = self.request.query_params.get("sequence")
        if seq:
            interval_ids = SurveyInterval.objects.filter(
                sequence__code=seq).values_list("id", flat=True)
            iv = SurveyInterval.objects.filter(sequence__code=seq)
            t1t2 = [(i.t1_campaign_id, i.t2_campaign_id) for i in iv]
            from django.db.models import Q
            q = Q()
            for a, b in t1t2:
                q |= Q(t1_campaign_id=a, t2_campaign_id=b)
            qs = qs.filter(q) if t1t2 else qs.none()
        interval_id = self.request.query_params.get("interval")
        if interval_id:
            iv = get_object_or_404(SurveyInterval, pk=interval_id)
            qs = qs.filter(t1_campaign=iv.t1_campaign,
                           t2_campaign=iv.t2_campaign)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Human-in-the-loop resolution. Nothing here is automatic.

        After the verdict is recorded, EVERY interval whose link contains
        the item is rebuilt, so the workbench, timeline and coverage
        snapshots reflect the decision. Frozen estimate editions are not
        touched (their payloads were frozen at confirmation).
        """
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
            if (decision == "renumber"
                    and conflict.hint != "gap_reappearance"
                    and conflict.t1_measurement_id):
                # same individual: t2's tree row becomes a successor of t1's.
                # NEVER done for gap items — verifying a reappearance does
                # not stitch the missing occasion into one tracked row.
                t2_tree = conflict.t2_measurement.tree
                t1_tree = conflict.t1_measurement.tree
                if t2_tree != t1_tree:
                    t2_tree.superseded_tree = t1_tree
                    t2_tree.current_field_number = (
                        conflict.t2_measurement.field_number_seen
                    )
                    t2_tree.save(update_fields=["superseded_tree",
                                               "current_field_number"])
            # distinct / gap verdicts: rows stay separate (gap stays
            # excluded — a human cannot conjure the missing occasion).
            conflict.status = decision
            conflict.resolution_note = ser.validated_data.get("note", "")
            conflict.resolved_at = timezone.now()
            conflict.save()

            rebuilt = []
            for iv in SurveyInterval.objects.filter(
                    t1_campaign=conflict.t1_campaign,
                    t2_campaign=conflict.t2_campaign):
                rebuilt.append(refresh_interval(iv))
        payload = ConflictSerializer(conflict).data
        payload["rebuilt_intervals"] = rebuilt
        return Response(payload)


# ============================================================== chain: imports
def _payload_fingerprint(campaign_code, rows):
    body = json.dumps(
        {"campaign": campaign_code,
         "rows": sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))},
        sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


class ImportViewSet(viewsets.ViewSet):
    def create(self, request):
        ser = MeasurementImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        campaign = get_object_or_404(
            Campaign, code=ser.validated_data["campaign"])
        rows = ser.validated_data["rows"]
        client_id = ser.validated_data.get("client_batch_id")
        fingerprint = _payload_fingerprint(campaign.code, rows)

        # Idempotency envelope: a retransmission carrying the same batch id,
        # or an identical payload retry, replays the STORED result — never a
        # second set of trees/audit rows/interval artefacts.
        batch = None
        if client_id:
            batch = ImportBatch.objects.filter(
                campaign=campaign, client_batch_id=client_id).first()
            if batch is not None and batch.payload_fingerprint != fingerprint:
                return Response(
                    {"detail": f"client_batch_id {client_id!r} was already "
                               "used with DIFFERENT rows; choose a new batch "
                               "id rather than overwriting an upload."},
                    status=status.HTTP_409_CONFLICT)
        if batch is None:
            batch = ImportBatch.objects.filter(
                campaign=campaign,
                payload_fingerprint=fingerprint).first()
        if batch is not None:
            summary = dict(batch.result_summary)
            summary["idempotent_replay"] = True
            summary["batch_id"] = batch.id
            return Response(
                summary,
                status=status.HTTP_200_OK)

        with transaction.atomic():
            batch = ImportBatch.objects.create(
                campaign=campaign,
                client_batch_id=(client_id
                                 or f"auto-{fingerprint[:24]}"),
                payload_fingerprint=fingerprint, n_rows=len(rows))
            result = import_campaign_rows(
                campaign, rows, settings.PLOT_AREA_TOLERANCE, batch=batch)

            # Rebuild ONLY the adjacent chain links that contain this
            # campaign. Non-adjacent occasions are never paired.
            refreshed = refresh_adjacent_intervals(campaign)

            # Legacy fallback for databases that pre-date sequences: still
            # scan the other existing campaign so the old two-period UI
            # works without a chain.
            if not refreshed:
                other = Campaign.objects.exclude(
                    pk=campaign.pk).order_by("measured_on").first()
                if other:
                    t1, t2 = sorted([campaign, other],
                                    key=lambda c: c.measured_on)
                    result["conflicts"] = scan_conflicts(t1, t2)

            result["batch_id"] = batch.id
            result["client_batch_id"] = batch.client_batch_id
            result["refreshed_intervals"] = refreshed
            batch.result_summary = {
                k: v for k, v in result.items()
                if k in ("campaign", "n_rows", "n_accepted", "n_rejected")}
            batch.save(update_fields=["result_summary"])

        return Response(
            result,
            status=status.HTTP_207_MULTI_STATUS if result["rejected"]
            else status.HTTP_200_OK)


class ImportBatchViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ImportBatch.objects.all().order_by("-created_at")
    serializer_class = ImportBatchSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        return qs


# ============================================================ chain: sequences
class SequenceViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = SurveySequence.objects.all().prefetch_related(
            "memberships__campaign").order_by("code")
        return Response(SequenceSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        seq = get_object_or_404(SurveySequence, pk=pk)
        return Response(SequenceSerializer(seq).data)

    def create(self, request):
        ser = SequenceCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        code = ser.validated_data["code"]
        if SurveySequence.objects.filter(code=code).exists():
            return Response(
                {"detail": f"sequence {code!r} already exists; POST "
                           "add_campaigns to extend it."},
                status=status.HTTP_409_CONFLICT)
        try:
            seq, _ = create_sequence(
                code,
                ser.validated_data.get("name") or code,
                ser.validated_data["campaigns"],
                status=ser.validated_data.get("status", "active"))
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        for iv in seq.intervals.all():
            refresh_interval(iv)
        seq.refresh_from_db()
        return Response(SequenceSerializer(seq).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="add_campaigns")
    def add_campaigns(self, request, pk=None):
        """Create/complete the chain: only NEW adjacent links are built."""
        seq = get_object_or_404(SurveySequence, pk=pk)
        ser = SequenceAddCampaignsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            out = add_campaigns(seq, ser.validated_data["campaigns"])
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        for iv in seq.intervals.filter(status="pending"):
            out.setdefault("built", []).append(refresh_interval(iv))
        return Response(out, status=status.HTTP_200_OK)


class IntervalViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = SurveyInterval.objects.select_related(
            "sequence", "t1_campaign", "t2_campaign")
        seq = request.query_params.get("sequence")
        if seq:
            qs = qs.filter(sequence__code=seq)
        t1 = request.query_params.get("t1")
        t2 = request.query_params.get("t2")
        if t1:
            qs = qs.filter(t1_campaign__code=t1)
        if t2:
            qs = qs.filter(t2_campaign__code=t2)
        qs = qs.order_by("sequence__code", "ordinal")
        return Response(IntervalSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        iv = get_object_or_404(SurveyInterval, pk=pk)
        return Response(IntervalSerializer(iv).data)

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        """(Re)materialise coverage + interval-scoped identity links."""
        iv = get_object_or_404(SurveyInterval, pk=pk)
        out = refresh_interval(iv)
        return Response(out)

    @action(detail=True, methods=["get"])
    def provenance(self, request, pk=None):
        iv = get_object_or_404(SurveyInterval, pk=pk)
        return Response(interval_provenance(iv))

    @action(detail=True, methods=["post"], url_path="estimates")
    def estimates(self, request, pk=None):
        """Run a STRICT gap-chain draft edition attached to this link."""
        iv = get_object_or_404(SurveyInterval, pk=pk)
        ser = IntervalEstimateRunSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            version = run_interval_estimate(
                iv, ser.validated_data["equation_ids"],
                label=ser.validated_data.get("label"),
                fpc=ser.validated_data.get("fpc", True))
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(EstimateVersionSerializer(version).data,
                        status=status.HTTP_201_CREATED)


class TimelineView(viewsets.ViewSet):
    """
    GET /api/timeline/?sequence=CODE&plot=P01
    GET /api/timeline/?sequence=CODE&tree=ID
    """

    def list(self, request):
        seq_code = request.query_params.get("sequence")
        if not seq_code:
            return Response({"detail": "sequence=CODE required"},
                            status=status.HTTP_400_BAD_REQUEST)
        seq = get_object_or_404(SurveySequence, code=seq_code)
        plot_code = request.query_params.get("plot")
        tree_id = request.query_params.get("tree")
        if tree_id:
            tree = get_object_or_404(Tree, pk=tree_id)
            return Response(tree_timeline(seq, tree))
        if plot_code:
            plot = get_object_or_404(Plot, code=plot_code)
            return Response(plot_timeline(seq, plot))
        return Response(
            {"detail": "plot=CODE or tree=ID required"},
            status=status.HTTP_400_BAD_REQUEST)


class EstimateViewSet(viewsets.ViewSet):
    """Estimate editions; confirm freezes forever. Legacy path retained."""

    def list(self, request):
        qs = EstimateVersion.objects.all().order_by("-created_at")
        interval_id = request.query_params.get("interval")
        if interval_id:
            qs = qs.filter(interval_id=interval_id)
        return Response(EstimateVersionSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(
            EstimateVersionSerializer(_get_version(pk)).data
        )

    def create(self, request):
        # Legacy two-campaign draft (non-strict) — behaviour unchanged.
        return run_legacy_estimate(request)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Freeze the edition forever and lock its equations."""
        version = _get_version(pk)
        if version.status == VERSION_CONFIRMED:
            return Response({"detail": "already confirmed"},
                            status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
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
            from inventory.services.estimator import equation_checksum
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
            from inventory.models import EQUATION_CONFIRMED
            eqs.update(status=EQUATION_CONFIRMED)
        return Response(EstimateVersionSerializer(version).data)


def _get_version(pk):
    return get_object_or_404(
        EstimateVersion.objects.prefetch_related("equations"), pk=pk)

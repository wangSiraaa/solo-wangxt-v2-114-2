from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    Campaign,
    EstimateVersion,
    IdentityConflict,
    ImportBatch,
    IntervalLink,
    Plot,
    Species,
    Stratum,
    SurveyInterval,
    SurveySequence,
    Tree,
    TreeMeasurement,
)


class StratumSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stratum
        fields = ["id", "code", "name", "area_ha"]


class SpeciesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Species
        fields = ["id", "code", "name", "family"]


class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ["id", "code", "measured_on", "description"]


class PlotSerializer(serializers.ModelSerializer):
    stratum_code = serializers.CharField(source="stratum.code", read_only=True)
    stratum_name = serializers.CharField(source="stratum.name", read_only=True)
    crs_epsg = serializers.SerializerMethodField()

    class Meta:
        model = Plot
        fields = [
            "id", "code", "stratum", "stratum_code", "stratum_name",
            "x_m", "y_m", "declared_area_ha", "area_polygon_ha",
            "boundary", "crs_epsg",
        ]

    def get_crs_epsg(self, _obj):
        from django.conf import settings
        return settings.SURVEY_CRS_EPSG


class EquationSerializer(serializers.ModelSerializer):
    species_codes = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="code", source="species"
    )

    class Meta:
        model = AllometricEquation
        fields = [
            "id", "code", "version", "species_codes", "status", "form",
            "a", "b", "c", "dbh_min_cm", "dbh_max_cm",
            "height_required", "residual_sigma", "citation", "created_at",
        ]


class TreeSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    species_code = serializers.CharField(source="species.code", read_only=True)
    supersedes = serializers.PrimaryKeyRelatedField(
        source="superseded_tree", read_only=True
    )

    class Meta:
        model = Tree
        fields = [
            "id", "plot", "plot_code", "species_code",
            "current_field_number", "first_campaign", "supersedes",
        ]


class MeasurementSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="tree.plot.code", read_only=True)
    field_number = serializers.CharField(source="field_number_seen")

    class Meta:
        model = TreeMeasurement
        fields = [
            "id", "tree", "campaign", "plot_code", "field_number",
            "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "notes",
        ]


class ConflictSerializer(serializers.ModelSerializer):
    t1_campaign_code = serializers.CharField(
        source="t1_campaign.code", read_only=True)
    t2_campaign_code = serializers.CharField(
        source="t2_campaign.code", read_only=True)
    plot_code = serializers.CharField(source="plot.code", read_only=True)

    class Meta:
        model = IdentityConflict
        fields = [
            "id", "plot", "plot_code", "field_number",
            "t1_campaign", "t2_campaign", "t1_campaign_code",
            "t2_campaign_code", "t1_measurement", "t2_measurement",
            "distance_m", "hint", "status", "resolution_note", "resolved_at",
        ]
        read_only_fields = ["distance_m", "resolved_at", "hint"]


class ConflictResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["renumber", "distinct"])
    note = serializers.CharField(required=False, allow_blank=True)


class EstimateVersionSerializer(serializers.ModelSerializer):
    interval_id = serializers.IntegerField(read_only=True)
    interval_code = serializers.SerializerMethodField()

    class Meta:
        model = EstimateVersion
        fields = [
            "id", "label", "t1_campaign", "t2_campaign", "interval_id",
            "interval_code", "status",
            "design_snapshot", "result_payload", "equation_checksum",
            "created_at", "confirmed_at",
        ]
        read_only_fields = [
            "status", "design_snapshot", "result_payload",
            "equation_checksum", "confirmed_at",
        ]

    def get_interval_code(self, obj):
        if obj.interval_id:
            return f"{obj.t1_campaign.code}->{obj.t2_campaign.code}"
        return None


class MeasurementImportRowSerializer(serializers.Serializer):
    """One raw field row. Units are mandatory with every value."""

    plot = serializers.CharField()
    field_number = serializers.CharField()
    species = serializers.CharField()
    x_m = serializers.FloatField()
    y_m = serializers.FloatField()
    status = serializers.ChoiceField(
        choices=["alive_measured", "alive_not_measured", "dead", "missing_tree"]
    )
    dbh_raw = serializers.FloatField(required=False, allow_null=True)
    dbh_unit = serializers.ChoiceField(choices=["cm", "mm", "in"],
                                       required=False, allow_null=True)
    height_raw = serializers.FloatField(required=False, allow_null=True)
    height_unit = serializers.ChoiceField(choices=["m"],
                                          required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # Used ONLY to record a field-book verified renumber. Never inferred.
    verified_renumber_of_tree = serializers.IntegerField(
        required=False, allow_null=True
    )


class MeasurementImportSerializer(serializers.Serializer):
    campaign = serializers.CharField()
    # Optional client idempotency key. Same campaign + same key returns the
    # stored batch instead of re-creating trees / conflicts / interval links.
    client_batch_id = serializers.CharField(
        required=False, allow_blank=False, max_length=120)
    rows = MeasurementImportRowSerializer(many=True)


# ------------------------------------------------------------- multi-campaign
class SequenceSerializer(serializers.ModelSerializer):
    campaigns = serializers.SerializerMethodField()
    n_intervals = serializers.SerializerMethodField()

    class Meta:
        model = SurveySequence
        fields = ["id", "code", "name", "status", "campaigns",
                  "n_intervals", "created_at"]

    def get_campaigns(self, obj):
        return [
            {"code": m.campaign.code,
             "measured_on": m.campaign.measured_on.isoformat(),
             "position": m.position}
            for m in sorted(obj.memberships.select_related("campaign"),
                            key=lambda m: m.position)
        ]

    def get_n_intervals(self, obj):
        return obj.intervals.count()


class SequenceCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=32)
    name = serializers.CharField(max_length=160, required=False,
                                 allow_blank=True)
    campaigns = serializers.ListField(
        child=serializers.CharField(), allow_empty=False)
    status = serializers.ChoiceField(["active", "closed"], required=False)


class SequenceAddCampaignsSerializer(serializers.Serializer):
    campaigns = serializers.ListField(
        child=serializers.CharField(), allow_empty=False)


class IntervalLinkSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)

    class Meta:
        model = IntervalLink
        fields = [
            "id", "plot", "plot_code", "t1_tree", "t2_tree",
            "t1_measurement", "t2_measurement",
            "t1_field_number", "t2_field_number",
            "kind", "determination", "excluded_from_components",
            "conflict", "detail", "created_at",
        ]


class IntervalEstimateSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = EstimateVersion
        fields = ["id", "label", "status", "created_at", "confirmed_at"]


class IntervalSerializer(serializers.ModelSerializer):
    sequence_code = serializers.CharField(source="sequence.code", read_only=True)
    t1_code = serializers.CharField(source="t1_campaign.code", read_only=True)
    t2_code = serializers.CharField(source="t2_campaign.code", read_only=True)
    code = serializers.SerializerMethodField()
    n_links = serializers.SerializerMethodField()
    n_pending_links = serializers.SerializerMethodField()
    estimate_versions = serializers.SerializerMethodField()

    class Meta:
        model = SurveyInterval
        fields = [
            "id", "code", "sequence", "sequence_code", "ordinal",
            "t1_campaign", "t2_campaign", "t1_code", "t2_code", "status",
            "coverage_snapshot", "built_at", "created_at",
            "n_links", "n_pending_links", "estimate_versions",
        ]

    def get_code(self, obj):
        return obj.code

    def get_n_links(self, obj):
        return obj.links.count()

    def get_n_pending_links(self, obj):
        from inventory.models import PENDING_LINK_KINDS
        return obj.links.filter(kind__in=PENDING_LINK_KINDS).count()

    def get_estimate_versions(self, obj):
        return IntervalEstimateSummarySerializer(
            obj.estimate_versions.all(), many=True).data


class IntervalEstimateRunSerializer(serializers.Serializer):
    label = serializers.CharField(required=False, allow_blank=True)
    equation_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False)
    fpc = serializers.BooleanField(required=False, default=True)


class ImportBatchSerializer(serializers.ModelSerializer):
    campaign_code = serializers.CharField(source="campaign.code", read_only=True)

    class Meta:
        model = ImportBatch
        fields = ["id", "campaign", "campaign_code", "client_batch_id",
                  "payload_fingerprint", "n_rows", "result_summary",
                  "created_at"]

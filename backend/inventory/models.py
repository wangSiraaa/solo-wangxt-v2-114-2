"""
Domain model for repeated-measure permanent forest plots.

Explicit design choices
=======================
* Every measured quantity carries its unit — the database never stores a
  "dbh number" without dbh_unit next to it. Accepted units: cm/mm/in for
  dbh, m for height. Unit conversion happens at ingest; canonical storage
  is centimetres (dbh) and metres (height).
* Tree coordinates are projected (x_m, y_m) in SURVEY_CRS_EPSG; plot
  boundaries are GeoJSON polygons in the same CRS. With PostGIS,
  deploy/postgis.sql adds generated geometry columns + GiST indexes.
* Individual tree identity across surveys is NOT assumed from equal tree
  numbers. Same number + contradictory position opens an IdentityConflict
  and the pair stays out of growth estimation until a human resolves it.
* Missing measurements (alive but not measured) are distinct from real
  zero growth (measured, |Δdbh| <= tolerance, cross-checked) and from
  mortality (a mortality observation exists).
* Allometric equations are versioned. EstimateVersion freezes the
  equations and a SHA-256 checksum; confirmed versions are locked and a
  new equation can never silently alter a published estimate.
"""
from django.db import models


# ---------------------------------------------------------------- constants
DBH_UNITS = ["cm", "mm", "in"]
HEIGHT_UNITS = ["m"]

DBH_TO_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54}

# Tree measurement status — three mutually exclusive situations that a
# careless field database tends to collapse into "dbh = 0".
STATUS_ALIVE_MEASURED = "alive_measured"
STATUS_ALIVE_NOT_MEASURED = "alive_not_measured"   # missing, NOT zero
STATUS_DEAD = "dead"                                # mortality
STATUS_MISSING = "missing_tree"                     # not located at all
TREE_STATUS_CHOICES = [
    (STATUS_ALIVE_MEASURED, "Alive and remeasured"),
    (STATUS_ALIVE_NOT_MEASURED, "Alive but not measured (missing data)"),
    (STATUS_DEAD, "Dead (mortality observation)"),
    (STATUS_MISSING, "Tree not located"),
]

CONFLICT_OPEN = "open"
CONFLICT_RENUMBER = "renumber"          # same tree, new number
CONFLICT_DISTINCT = "distinct"          # genuinely different individuals
CONFLICT_CHOICES = [
    (CONFLICT_OPEN, "Unverified — excluded from estimates"),
    (CONFLICT_RENUMBER, "Verified renumber (same individual)"),
    (CONFLICT_DISTINCT, "Verified distinct individuals"),
]

EQUATION_DRAFT = "draft"
EQUATION_CONFIRMED = "confirmed"
EQUATION_RETIRED = "retired"
EQUATION_STATUS_CHOICES = [
    (EQUATION_DRAFT, "Draft (not usable for estimates)"),
    (EQUATION_CONFIRMED, "Confirmed (locked when used)"),
    (EQUATION_RETIRED, "Retired"),
]

VERSION_DRAFT = "draft"
VERSION_CONFIRMED = "confirmed"
VERSION_SUPERSEDED = "superseded"
VERSION_STATUS_CHOICES = [
    (VERSION_DRAFT, "Draft — recomputes with current data/equations"),
    (VERSION_CONFIRMED, "Confirmed — frozen, immutable"),
    (VERSION_SUPERSEDED, "Superseded by a newer confirmed version"),
]

# ------------------------------------------------------- multi-period chain
# Interval coverage: does the remeasurement actually reach both endpoints?
COVERAGE_PENDING = "pending"    # far end not measured yet (planned campaign)
COVERAGE_PARTIAL = "partial"    # some plots measured at both ends
COVERAGE_COVERED = "covered"    # every plot measured at both ends
COVERAGE_CHOICES = [
    (COVERAGE_PENDING, "Pending — far-end campaign has no data yet"),
    (COVERAGE_PARTIAL, "Partial — some plots lack measurements"),
    (COVERAGE_COVERED, "Covered — every plot measured at both ends"),
]

# Per-interval identity determinations. A link is the interval-scoped
# statement "this individual plays this role between t1 and t2". Identity
# relations NEVER span more than one interval: a tree missing at the middle
# campaign and reappearing later is a pending gap link, not a survivor.
LINK_SURVIVOR = "survivor"                       # paired, measured both ends
LINK_SURVIVOR_RENUMBER = "survivor_renumber"     # paired via verified renumber
LINK_SURVIVOR_UNMEASURED = "survivor_unmeasured"  # alive, dbh missing one end
LINK_MORTALITY = "mortality"
LINK_NOT_LOCATED = "not_located"                 # missing_tree / vanished
LINK_INGROWTH = "ingrowth"
LINK_BELOW_RECRUITMENT = "below_recruitment"
LINK_GAP_REAPPEARANCE = "gap_reappearance"   # missing at t1, alive at t2
LINK_RESURRECTED = "resurrected"             # dead at t1, alive at t2
LINK_IDENTITY_CONFLICT = "identity_conflict"  # open contradiction this interval
LINK_KIND_CHOICES = [
    (LINK_SURVIVOR, "Survivor (measured both ends)"),
    (LINK_SURVIVOR_RENUMBER, "Survivor via verified renumber"),
    (LINK_SURVIVOR_UNMEASURED, "Survivor with missing measurement"),
    (LINK_MORTALITY, "Mortality"),
    (LINK_NOT_LOCATED, "Not located"),
    (LINK_INGROWTH, "Ingrowth (reached threshold this interval)"),
    (LINK_BELOW_RECRUITMENT, "Below recruitment threshold"),
    (LINK_GAP_REAPPEARANCE, "PENDING — reappeared after a missing occasion"),
    (LINK_RESURRECTED, "PENDING — recorded dead, later alive"),
    (LINK_IDENTITY_CONFLICT, "PENDING — unverified identity this interval"),
]
PENDING_LINK_KINDS = {
    LINK_GAP_REAPPEARANCE,
    LINK_RESURRECTED,
    LINK_IDENTITY_CONFLICT,
}


class Stratum(models.Model):
    """Sampling stratum with known land area (the sampling frame)."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=120)
    area_ha = models.FloatField(
        help_text="Known stratum land area in hectares (sampling frame)."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} ({self.name})"


class Plot(models.Model):
    """A permanent sample plot. Area is per plot, not assumed constant."""

    code = models.CharField(max_length=16, unique=True)
    stratum = models.ForeignKey(
        Stratum, on_delete=models.PROTECT, related_name="plots"
    )
    # Projected coordinates of plot centre, metres.
    x_m = models.FloatField(help_text="Plot centre X, projected CRS [m].")
    y_m = models.FloatField(help_text="Plot centre Y, projected CRS [m].")
    declared_area_ha = models.FloatField(
        help_text="Declared plot area in hectares. Plots may differ in size."
    )
    # GeoJSON-like ring: [[x, y], ...] in projected metres.
    boundary = models.JSONField(
        help_text="Boundary ring [[x_m, y_m], ...] in projected CRS."
    )
    area_polygon_ha = models.FloatField(
        help_text="Polygon area in ha, computed at ingest and cross-checked."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Species(models.Model):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=160)
    family = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name_plural = "species"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"


class AllometricEquation(models.Model):
    """
    Versioned allometric biomass equation.

        agb_kg = a * (dbh_cm ** b) * (height_m ** c)

    Applicability is explicit (species list, dbh range, source/citation).
    Residual error is stored as a dimensionless multiplicative sigma
    (agb * exp(eps), eps ~ N(0, residual_sigma^2)) and propagated into
    equation-error components of uncertainty.
    """

    code = models.CharField(max_length=32)
    version = models.CharField(max_length=16)
    species = models.ManyToManyField(Species, related_name="equations")
    status = models.CharField(
        max_length=16, choices=EQUATION_STATUS_CHOICES, default=EQUATION_DRAFT
    )
    form = models.CharField(
        max_length=64,
        default="agb = a * dbh_cm^b * height_m^c",
        help_text="Human-readable equation form.",
    )
    a = models.FloatField()
    b = models.FloatField()
    c = models.FloatField()
    dbh_min_cm = models.FloatField()
    dbh_max_cm = models.FloatField()
    height_required = models.BooleanField(default=True)
    residual_sigma = models.FloatField(
        help_text="Multiplicative residual SD of ln(agb) [dimensionless]."
    )
    citation = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code", "version"]
        unique_together = [("code", "version")]

    @property
    def coefficient_checksum_source(self):
        return (
            f"{self.code}|{self.version}|{self.a:.10g}|{self.b:.10g}|"
            f"{self.c:.10g}|{self.dbh_min_cm:.6g}|{self.dbh_max_cm:.6g}|"
            f"{self.residual_sigma:.6g}"
        )

    _FROZEN_FIELDS = ("code", "version", "a", "b", "c", "dbh_min_cm",
                      "dbh_max_cm", "residual_sigma", "form",
                      "height_required")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == EQUATION_CONFIRMED:
                changed = [
                    f for f in self._FROZEN_FIELDS
                    if getattr(original, f) != getattr(self, f)
                ]
                if changed:
                    raise PermissionError(
                        f"Equation {self.code} v{self.version} is locked by a "
                        f"confirmed estimate; changed fields {changed}. Issue "
                        "a new equation code/version instead."
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} v{self.version} [{self.status}]"


class Campaign(models.Model):
    """One measurement occasion (survey edition)."""

    code = models.CharField(max_length=16, unique=True)
    measured_on = models.DateField()
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["measured_on"]

    def __str__(self):
        return f"{self.code} ({self.measured_on})"


class Tree(models.Model):
    """
    A tracked individual. The field number is a label, not a primary key:
    renumbers keep the same Tree row (current_field_number changes), while
    a same-number/different-location finding becomes a second Tree row plus
    an IdentityConflict until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT, related_name="trees")
    current_field_number = models.CharField(max_length=16)
    species = models.ForeignKey(
        Species, on_delete=models.PROTECT, related_name="trees"
    )
    first_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="trees_first"
    )
    superseded_tree = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="successors",
        help_text="Set when this row re-numbers an earlier tree (verified).",
    )

    class Meta:
        # Field numbers are LABELS, not identities: a renumber keeps one
        # Tree row, a same-number/different-position finding creates two
        # Tree rows carrying the same label in one plot. The authoritative
        # label per occasion is TreeMeasurement.field_number_seen.
        indexes = [
            models.Index(fields=["plot", "current_field_number"]),
        ]
        ordering = ["plot__code", "current_field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.current_field_number}"


class TreeMeasurement(models.Model):
    """One tree measured (or sought and found dead/missing) at one campaign."""

    tree = models.ForeignKey(
        Tree, on_delete=models.PROTECT, related_name="measurements"
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="measurements"
    )
    field_number_seen = models.CharField(
        max_length=16,
        help_text="Number physically on the tag at this campaign.",
    )
    x_m = models.FloatField(help_text="Stem position X, projected CRS [m].")
    y_m = models.FloatField(help_text="Stem position Y, projected CRS [m].")
    status = models.CharField(max_length=20, choices=TREE_STATUS_CHOICES)

    # Raw entry keeps the original unit; canonical *_cm / *_m columns are
    # converted at ingest. Both are retained so a unit error is auditable.
    dbh_raw = models.FloatField(null=True, blank=True)
    dbh_unit = models.CharField(max_length=2, null=True, blank=True,
                                choices=[(u, u) for u in DBH_UNITS])
    dbh_cm = models.FloatField(null=True, blank=True)

    height_raw = models.FloatField(null=True, blank=True)
    height_unit = models.CharField(max_length=2, null=True, blank=True,
                                   choices=[(u, u) for u in HEIGHT_UNITS])
    height_m = models.FloatField(null=True, blank=True)

    notes = models.CharField(max_length=240, blank=True)

    class Meta:
        unique_together = [("tree", "campaign")]
        ordering = ["tree__plot__code", "tree__current_field_number"]

    def __str__(self):
        return f"{self.tree} @ {self.campaign.code}: {self.status}"


class IdentityConflict(models.Model):
    """
    Same field number at both campaigns, but positions contradict the
    hypothesis "same individual". Never auto-merged: stays open (and the
    trees are excluded from survivor growth) until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT)
    field_number = models.CharField(max_length=16)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t2"
    )
    t1_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t1"
    )
    t2_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t2"
    )
    distance_m = models.FloatField(
        help_text="Distance between the two reported positions [m]."
    )
    status = models.CharField(
        max_length=10, choices=CONFLICT_CHOICES, default=CONFLICT_OPEN
    )
    resolution_note = models.CharField(max_length=240, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plot__code", "field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.field_number}: {self.status}"


class MeasurementImportRow(models.Model):
    """Audit trail for raw imported rows, including rejected ones."""

    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="import_rows"
    )
    plot = models.ForeignKey(
        Plot, on_delete=models.PROTECT, related_name="import_rows"
    )
    raw_payload = models.JSONField()
    accepted = models.BooleanField()
    rejection_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class EstimateVersion(models.Model):
    """
    An immutable edition of the population estimate.

    On confirmation:
      * status -> confirmed (edits to the row are blocked in save());
      * equations referenced get locked;
      * result_payload + equation_checksum are frozen.
    A later new allometric equation creates a NEW version; the confirmed
    one can never be silently changed.

    ``interval`` links the edition to one adjacent survey interval. Legacy
    editions created before sequences existed keep interval=NULL and are
    matched to intervals read-time by their (t1, t2) campaigns — they are
    never rewritten.
    """

    label = models.CharField(max_length=120)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t2"
    )
    interval = models.ForeignKey(
        "SurveyInterval", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="estimate_versions",
        help_text="Adjacent survey interval this edition belongs to.",
    )
    equations = models.ManyToManyField(AllometricEquation, related_name="estimates")
    status = models.CharField(
        max_length=12, choices=VERSION_STATUS_CHOICES, default=VERSION_DRAFT
    )
    design_snapshot = models.JSONField(
        help_text="Stratum areas, expansion design, thresholds, CRS, "
                  "uncertainty assumptions used for this run."
    )
    result_payload = models.JSONField(
        null=True, blank=True,
        help_text="Full component breakdown + provenance + uncertainty."
    )
    equation_checksum = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == VERSION_CONFIRMED:
                raise PermissionError(
                    "EstimateVersion is confirmed/frozen; create a new "
                    "version instead of mutating this one."
                )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.label} [{self.status}]"


# ---------------------------------------------------------------------------
# Multi-period survey sequences: an ordered chain of campaigns with one
# traceable interval per ADJACENT pair. Identity relations, coverage and
# estimate editions are stored per interval; nothing ever stitches t1 of one
# interval directly to t2 of another.
# ---------------------------------------------------------------------------
class SurveySequence(models.Model):
    """An ordered chain of measurement campaigns (the remeasurement series)."""

    name = models.CharField(max_length=80, unique=True)
    campaigns = models.ManyToManyField(
        Campaign, through="SequenceCampaign", related_name="sequences"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def ordered_campaigns(self):
        return [m.campaign for m in
                self.memberships.select_related("campaign").order_by("position")]

    def __str__(self):
        return self.name


class SequenceCampaign(models.Model):
    """Membership of a campaign in a sequence, with its chain position."""

    sequence = models.ForeignKey(
        SurveySequence, on_delete=models.CASCADE, related_name="memberships"
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="sequence_memberships"
    )
    position = models.PositiveIntegerField()

    class Meta:
        unique_together = [("sequence", "campaign"), ("sequence", "position")]
        ordering = ["position"]

    def __str__(self):
        return f"{self.sequence.name}#{self.position}: {self.campaign.code}"


class SurveyInterval(models.Model):
    """
    One adjacent campaign pair inside a sequence (e.g. 2024 -> 2029).

    Stores its own coverage status, identity determinations (via
    IntervalIdentityLink) and estimate editions (EstimateVersion.interval),
    so 2019->2024 and 2024->2029 each carry their own growth / mortality /
    ingrowth / pending items. Intervals are created idempotently
    (unique per sequence+t1+t2) and never deleted by sync — a pair that
    stops being adjacent is kept with is_adjacent=False for traceability.
    """

    sequence = models.ForeignKey(
        SurveySequence, on_delete=models.CASCADE, related_name="intervals"
    )
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="intervals_as_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="intervals_as_t2"
    )
    position = models.PositiveIntegerField(
        help_text="Chain order of this interval at creation."
    )
    coverage = models.CharField(
        max_length=10, choices=COVERAGE_CHOICES, default=COVERAGE_PENDING
    )
    is_adjacent = models.BooleanField(
        default=True,
        help_text="False when a later campaign insertion made this pair "
                  "non-adjacent; kept for traceability, never rewritten.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("sequence", "t1_campaign", "t2_campaign")]
        ordering = ["sequence", "position"]

    @property
    def interval_years(self):
        days = (self.t2_campaign.measured_on
                - self.t1_campaign.measured_on).days
        return round(days / 365.25, 3)

    def __str__(self):
        return (f"{self.sequence.name}: {self.t1_campaign.code}→"
                f"{self.t2_campaign.code} [{self.coverage}]")


class IntervalIdentityLink(models.Model):
    """
    The identity determination of ONE individual within ONE interval.

    Persisted per interval so a pending verification chain survives
    recomputation and is visible per interval: gap reappearances, possible
    renumbers and position contradictions stay open links until a human
    resolves the corresponding IdentityConflict. Rebuilt idempotently
    (unique per interval+tree) — re-imports and retries never duplicate.
    """

    interval = models.ForeignKey(
        SurveyInterval, on_delete=models.CASCADE, related_name="identity_links"
    )
    tree = models.ForeignKey(
        Tree, on_delete=models.CASCADE, related_name="interval_links"
    )
    counterpart_tree = models.ForeignKey(
        Tree, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
        help_text="The other tree row involved (conflict/renumber pairs).",
    )
    t1_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+"
    )
    t2_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+"
    )
    kind = models.CharField(max_length=24, choices=LINK_KIND_CHOICES)
    pending = models.BooleanField(
        default=False,
        help_text="True while human verification is required; pending links "
                  "are excluded from every estimate component.",
    )
    note = models.CharField(max_length=240, blank=True)

    class Meta:
        unique_together = [("interval", "tree")]
        ordering = ["interval", "tree__plot__code",
                    "tree__current_field_number"]

    def __str__(self):
        return (f"{self.interval} · {self.tree}: {self.kind}"
                f"{' (pending)' if self.pending else ''}")

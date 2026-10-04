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

# Why an identity item is held for human verification. The hint is scoped to
# ONE adjacent interval: a 2029 near-neighbour renumber only ever flags the
# 2024->2029 interval, never 2019->2024.
HINT_SAME_NUMBER_MISMATCH = "same_number_position_mismatch"
HINT_LABEL_EXTRA_ROW = "same_label_extra_row"
HINT_POSSIBLE_RENUMBER = "possible_renumber"
# Tree was absent/not located at the interval's t1 occasion but already had a
# life at an EARLIER campaign (or t1 says missing_tree) and shows up alive at
# t2. Identity across such a gap is never auto-stitched into survivor growth.
HINT_GAP_REAPPEARANCE = "gap_reappearance"
GAP_HINTS = {HINT_GAP_REAPPEARANCE}
CONFLICT_HINT_CHOICES = [
    (HINT_SAME_NUMBER_MISMATCH, "Same number, contradictory position"),
    (HINT_LABEL_EXTRA_ROW, "Extra row reusing a label"),
    (HINT_POSSIBLE_RENUMBER, "New number near an old position"),
    (HINT_GAP_REAPPEARANCE, "Reappears after a missing occasion (gap)"),
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
    An identity item scoped to ONE adjacent interval (t1_campaign ->
    t2_campaign). Identity relations never span across a gap: the same field
    number at both campaigns with contradictory positions, a new number at a
    familiar position, or a tree that was missing/absent at t1 and reappears
    at t2 (``gap_reappearance``). Never auto-merged: stays open (and the
    trees are excluded from the interval's components) until verified.
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
        TreeMeasurement, on_delete=models.PROTECT,
        related_name="conflicts_as_t1", null=True, blank=True,
        help_text="Null when nothing for this tree exists at t1 (gap).",
    )
    t2_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t2"
    )
    distance_m = models.FloatField(
        null=True, blank=True,
        help_text="Distance between the two reported positions [m]; null "
                  "when one side has no position in this interval.",
    )
    hint = models.CharField(
        max_length=32, choices=CONFLICT_HINT_CHOICES,
        default=HINT_SAME_NUMBER_MISMATCH,
        help_text="Machine-readable reason this item is held.",
    )
    status = models.CharField(
        max_length=10, choices=CONFLICT_CHOICES, default=CONFLICT_OPEN
    )
    resolution_note = models.CharField(max_length=240, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plot__code", "field_number"]

    def __str__(self):
        return (f"{self.plot.code}/{self.field_number} "
                f"[{self.t1_campaign.code}->{self.t2_campaign.code}]: "
                f"{self.status}")


class MeasurementImportRow(models.Model):
    """Audit trail for raw imported rows, including rejected ones."""

    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="import_rows"
    )
    plot = models.ForeignKey(
        Plot, on_delete=models.PROTECT, related_name="import_rows"
    )
    batch = models.ForeignKey(
        "ImportBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="rows",
        help_text="Idempotency envelope; retransmissions reuse the batch.",
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
    """

    label = models.CharField(max_length=120)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t2"
    )
    interval = models.ForeignKey(
        "SurveyInterval", on_delete=models.PROTECT, null=True, blank=True,
        related_name="estimate_versions",
        help_text="Chain link this edition belongs to. Legacy two-campaign "
                  "editions leave this NULL and are never back-filled.",
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


# ============================================================== multi-campaign
# A survey sequence is an ORDERED chain of measurement occasions. Estimates
# and identity relations only ever exist on a single LINK of the chain (an
# interval between two ADJACENT occasions). 2019 and 2029 are never paired
# directly: a tree missing in 2024 and seen again in 2029 leaves a pending
# gap item on the 2024->2029 link instead of becoming survivor growth.

SEQUENCE_ACTIVE = "active"
SEQUENCE_CLOSED = "closed"
SEQUENCE_STATUS_CHOICES = [
    (SEQUENCE_ACTIVE, "Active — new remeasurements may be appended"),
    (SEQUENCE_CLOSED, "Closed"),
]

INTERVAL_PENDING = "pending"     # created, not yet materialised from data
INTERVAL_BUILT = "built"         # coverage snapshot + identity links built
INTERVAL_STATUS_CHOICES = [
    (INTERVAL_PENDING, "Pending — links not yet built"),
    (INTERVAL_BUILT, "Built — coverage/identity materialised"),
]

# IntervalLink.kind — the per-tree verdict WITHIN one interval.
# Survivor-side identity (a tracked individual seen at both ends):
LINK_SAME_NUMBER = "survivor_same_number"
LINK_RENUMBER = "survivor_renumber"
LINK_ZERO_GROWTH = "survivor_zero_growth"
# End-of-interval component fates (also single-interval facts):
LINK_NOT_MEASURED = "alive_not_measured"
LINK_MORTALITY = "mortality"
LINK_INGROWTH = "ingrowth"
LINK_BELOW_RECRUITMENT = "below_recruitment"
LINK_MISSING_T2 = "not_located_t2"
LINK_REMOVED_UNOBSERVED = "removal_unobserved"
LINK_DEAD_AT_T1 = "dead_at_t1"
LINK_RECRUIT_DISTINCT = "ingrowth_distinct_verified"
# Pending chain items — excluded from every component until human-verified:
LINK_PENDING_SAME_NUMBER = "pending_same_number_mismatch"
LINK_PENDING_RENUMBER = "possible_renumber"
LINK_PENDING_GAP = "gap_reappearance"
LINK_VERIFIED_GAP = "gap_reappearance_verified"
LINK_KIND_CHOICES = [
    (LINK_SAME_NUMBER, "Survivor — same tag"),
    (LINK_RENUMBER, "Survivor — verified renumber"),
    (LINK_ZERO_GROWTH, "Survivor — verified zero growth"),
    (LINK_NOT_MEASURED, "Alive at t2 but not measured"),
    (LINK_MORTALITY, "Mortality"),
    (LINK_INGROWTH, "Ingrowth"),
    (LINK_BELOW_RECRUITMENT, "Below recruitment — recorded, excluded"),
    (LINK_MISSING_T2, "Not located at t2"),
    (LINK_REMOVED_UNOBSERVED, "Present t1 only — removal unobserved"),
    (LINK_DEAD_AT_T1, "Already dead at t1 — no component this interval"),
    (LINK_RECRUIT_DISTINCT, "Distinct recruit verified"),
    (LINK_PENDING_SAME_NUMBER, "PENDING: same number, position mismatch"),
    (LINK_PENDING_RENUMBER, "PENDING: possible unrecorded renumber"),
    (LINK_PENDING_GAP, "PENDING: reappears after a gap (verify chain)"),
    (LINK_VERIFIED_GAP, "Gap reappearance human-verified (kept excluded)"),
]
PENDING_LINK_KINDS = {
    LINK_PENDING_SAME_NUMBER, LINK_PENDING_RENUMBER, LINK_PENDING_GAP,
}
GAP_LINK_KINDS = {LINK_PENDING_GAP, LINK_VERIFIED_GAP}


class SurveySequence(models.Model):
    """An ordered series of campaigns (2019, 2024, 2029, ...)."""

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=160)
    status = models.CharField(
        max_length=10, choices=SEQUENCE_STATUS_CHOICES, default=SEQUENCE_ACTIVE
    )
    campaigns = models.ManyToManyField(
        Campaign, through="SequenceMembership", related_name="sequences"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} [{self.status}]"


class SequenceMembership(models.Model):
    """A campaign's position in a sequence. Ordering is by ``position``."""

    sequence = models.ForeignKey(
        SurveySequence, on_delete=models.PROTECT, related_name="memberships"
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="memberships"
    )
    position = models.PositiveIntegerField(
        help_text="0-based order of the campaign inside the sequence."
    )

    class Meta:
        ordering = ["sequence", "position"]
        unique_together = [("sequence", "campaign"), ("sequence", "position")]

    def __str__(self):
        return f"{self.sequence.code}#{self.position}:{self.campaign.code}"


class SurveyInterval(models.Model):
    """
    One traceable link between two ADJACENT campaigns of a sequence.

    Owns three independently-versioned artefacts:
      * ``coverage_snapshot`` — how every tree was covered at each end plus a
        data fingerprint (status, n rows, per-plot/per-status counts, hash);
      * IntervalLink rows — the per-tree identity verdicts for THIS interval;
      * EstimateVersion rows — growth/mortality/ingrowth editions, linked
        back here. Old confirmed editions are never rewritten.
    """

    sequence = models.ForeignKey(
        SurveySequence, on_delete=models.PROTECT, related_name="intervals"
    )
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="intervals_as_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="intervals_as_t2"
    )
    ordinal = models.PositiveIntegerField(
        help_text="0-based link index: 0 = first pair of occasions."
    )
    status = models.CharField(
        max_length=10, choices=INTERVAL_STATUS_CHOICES, default=INTERVAL_PENDING
    )
    coverage_snapshot = models.JSONField(
        null=True, blank=True,
        help_text="Coverage state at both ends + fingerprint "
                  "{status, fingerprint, n_t1, n_t2, per_plot, built_at}.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    built_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sequence", "ordinal"]
        unique_together = [
            ("sequence", "t1_campaign", "t2_campaign"),
            ("sequence", "ordinal"),
        ]

    @property
    def code(self):
        return f"{self.t1_campaign.code}->{self.t2_campaign.code}"

    def __str__(self):
        return f"{self.sequence.code}: {self.code} [{self.status}]"


class IntervalLink(models.Model):
    """
    The identity verdict and component fate of ONE tracked tree within ONE
    interval. A verdict is valid only for this interval — the same tree on a
    later interval gets its own link. ``determination`` is who/what settled
    it: data (same tree row), a human conflict resolution, or nobody yet.
    """

    DETERMINED_DATA = "data"
    DETERMINED_HUMAN = "human"
    DETERMINED_PENDING = "pending"
    DETERMINATION_CHOICES = [
        (DETERMINED_DATA, "Determined by tracked tree row"),
        (DETERMINED_HUMAN, "Determined by human verification"),
        (DETERMINED_PENDING, "Undetermined — held for verification"),
    ]

    interval = models.ForeignKey(
        SurveyInterval, on_delete=models.PROTECT, related_name="links"
    )
    plot = models.ForeignKey(Plot, on_delete=models.PROTECT)
    t1_tree = models.ForeignKey(
        Tree, on_delete=models.PROTECT, null=True, blank=True,
        related_name="interval_links_as_t1",
    )
    t2_tree = models.ForeignKey(
        Tree, on_delete=models.PROTECT, null=True, blank=True,
        related_name="interval_links_as_t2",
    )
    t1_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, null=True, blank=True,
        related_name="interval_links_as_t1",
    )
    t2_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, null=True, blank=True,
        related_name="interval_links_as_t2",
    )
    t1_field_number = models.CharField(max_length=16, blank=True)
    t2_field_number = models.CharField(max_length=16, blank=True)
    kind = models.CharField(max_length=32, choices=LINK_KIND_CHOICES)
    determination = models.CharField(
        max_length=10, choices=DETERMINATION_CHOICES,
        default=DETERMINED_PENDING,
    )
    excluded_from_components = models.BooleanField(default=False)
    conflict = models.ForeignKey(
        IdentityConflict, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="interval_links",
    )
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["interval", "plot__code", "t2_field_number",
                    "t1_field_number"]
        indexes = [
            models.Index(fields=["interval", "kind"]),
            models.Index(fields=["t2_tree"]),
            models.Index(fields=["t1_tree"]),
        ]

    def __str__(self):
        return f"{self.interval}: {self.plot_id}/{self.kind}"


class ImportBatch(models.Model):
    """
    Idempotency envelope for a campaign upload. The client-supplied
    ``client_batch_id`` (or a hash of the payload) makes a RE-TRANSMISSION
    or a FAILED-RETRY return the same stored result without creating a second
    set of audit rows, trees or interval artefacts.
    """

    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="import_batches"
    )
    client_batch_id = models.CharField(max_length=120)
    payload_fingerprint = models.CharField(max_length=64)
    n_rows = models.PositiveIntegerField(default=0)
    result_summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("campaign", "client_batch_id")]

    def __str__(self):
        return f"{self.campaign.code}/batch/{self.client_batch_id}"

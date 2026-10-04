"""
Legacy two-campaign draft estimate endpoint.

Kept byte-for-byte in behaviour with the original station workflow: it does
NOT run strict gap chaining and does NOT attach a SurveyInterval, so the
existing 2019->2024 drafts/confirmations are unaffected by the multi-period
upgrade. Chain intervals run their STRICT edition via
``services.sequences.run_interval_estimate`` instead.
"""
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from inventory.models import AllometricEquation, Campaign, EstimateVersion
from inventory.serializers import EstimateVersionSerializer
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)


def run_legacy_estimate(request):
    """
    Body: {"label": ..., "t1_campaign": CODE, "t2_campaign": CODE,
           "equation_ids": [...], "fpc": true}
    Creates (or recomputes) a DRAFT. Confirmation is a separate action.
    """
    label = request.data.get("label", "draft estimate")
    t1 = Campaign.objects.filter(
        code=request.data.get("t1_campaign")).first()
    t2 = Campaign.objects.filter(
        code=request.data.get("t2_campaign")).first()
    if not t1 or not t2 or t1.measured_on >= t2.measured_on:
        return Response(
            {"detail": "need t1 earlier than t2 campaign codes"},
            status=status.HTTP_400_BAD_REQUEST)
    eq_ids = request.data.get("equation_ids", [])
    equations_qs = AllometricEquation.objects.filter(
        id__in=eq_ids
    ).prefetch_related("species")
    if list(equations_qs.values_list("id", flat=True)) != list(eq_ids) \
            or not eq_ids:
        return Response({"detail": "equation_ids invalid/empty"},
                        status=status.HTTP_400_BAD_REQUEST)

    table_t1, table_t2, equations, plots, strata = (
        build_measurement_table(t1, t2, equations_qs)
    )
    uncovered = sorted({
        r["species"] for r in table_t1 + table_t2
        if r["species"] not in equations
    })
    renumber, distinct = resolved_identity_pairs(t1, t2)

    interval = round(
        (t2.measured_on - t1.measured_on).days / 365.25, 3)
    design = {
        "t1_code": t1.code, "t2_code": t2.code,
        "interval_years": interval,
        "dbh_sd_cm": settings.DBH_MEASUREMENT_SD_CM,
        "height_sd_m": settings.HEIGHT_MEASUREMENT_SD_M,
        "zero_tol_cm": settings.ZERO_GROWTH_TOL_CM,
        "recruitment_cm": settings.RECRUITMENT_DBH_CM,
        "fpc": bool(request.data.get("fpc", True)),
        "crs_epsg": settings.SURVEY_CRS_EPSG,
        "strict_gap_chain": False,
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
                       "equation_ids": sorted(eq_ids),
                       "equation_codes": {sp: e["code"] + "@" + e["version"]
                                          for sp, e in equations.items()},
                       "area_tolerance": settings.PLOT_AREA_TOLERANCE}

    version = EstimateVersion.objects.create(
        label=label, t1_campaign=t1, t2_campaign=t2,
        design_snapshot=design_snapshot,
        result_payload=result, equation_checksum=checksum,
    )
    version.equations.set(equations_qs)
    return Response(EstimateVersionSerializer(version).data,
                    status=status.HTTP_201_CREATED)

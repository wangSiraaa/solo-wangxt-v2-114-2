from django.urls import include, path
from rest_framework.routers import DefaultRouter

from inventory import views

router = DefaultRouter()
router.register("strata", views.StratumViewSet)
router.register("species", views.SpeciesViewSet)
router.register("campaigns", views.CampaignViewSet)
router.register("equations", views.EquationViewSet)
router.register("plots", views.PlotViewSet)
router.register("trees", views.TreeViewSet, basename="tree")
router.register("measurements", views.MeasurementViewSet,
                basename="measurement")
router.register("conflicts", views.ConflictViewSet, basename="conflict")
router.register("imports", views.ImportViewSet, basename="import")
router.register("import-batches", views.ImportBatchViewSet,
                basename="import_batch")
router.register("estimates", views.EstimateViewSet, basename="estimate")
# Multi-campaign chains
router.register("sequences", views.SequenceViewSet, basename="sequence")
router.register("intervals", views.IntervalViewSet, basename="interval")

urlpatterns = [
    path("", include(router.urls)),
    path("timeline/", views.TimelineView.as_view({"get": "list"}),
         name="timeline"),
]

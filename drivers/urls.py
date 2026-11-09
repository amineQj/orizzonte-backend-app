"""
drivers/urls.py
~~~~~~~~~~~~~~~

Mounted under ``/api/`` by ``demenagement/urls.py``.

Driver (role driver):
    GET    /api/drivers/me/profile/
    PATCH  /api/drivers/me/profile/
    POST   /api/drivers/me/documents/
    POST   /api/drivers/me/submit/

Documents (owner, boss or agent):
    GET    /api/drivers/<id>/documents/<type>/

Boss:
    GET    /api/drivers/pending/
    GET    /api/drivers/<id>/
    POST   /api/drivers/<id>/approve/
    POST   /api/drivers/<id>/reject/

AI agent (X-Agent-Key):
    GET    /api/agent/drivers/pending/
    GET    /api/agent/drivers/<id>/
    GET    /api/agent/drivers/<id>/documents/<type>/
    POST   /api/agent/drivers/<id>/review/
"""
from django.urls import path

from .views import (
    AgentDriverDetailView,
    AgentDriverDocumentView,
    AgentPendingDriversView,
    AgentDriverReviewView,
    ApproveDriverView,
    BossDriverDetailView,
    DriverDocumentUploadView,
    DriverDocumentView,
    DriverProfileView,
    DriverSubmitView,
    PendingDriversView,
    RejectDriverView,
)

urlpatterns = [
    # ── Driver ──────────────────────────────────────────────────────────
    path("drivers/me/profile/", DriverProfileView.as_view(), name="driver-profile"),
    path("drivers/me/documents/", DriverDocumentUploadView.as_view(), name="driver-document-upload"),
    path("drivers/me/submit/", DriverSubmitView.as_view(), name="driver-submit"),

    # ── Boss ────────────────────────────────────────────────────────────
    path("drivers/pending/", PendingDriversView.as_view(), name="driver-pending"),
    path("drivers/<int:pk>/documents/<str:doc_type>/", DriverDocumentView.as_view(), name="driver-document"),
    path("drivers/<int:pk>/approve/", ApproveDriverView.as_view(), name="driver-approve"),
    path("drivers/<int:pk>/reject/", RejectDriverView.as_view(), name="driver-reject"),
    path("drivers/<int:pk>/", BossDriverDetailView.as_view(), name="driver-detail"),

    # ── AI agent ────────────────────────────────────────────────────────
    path("agent/drivers/pending/", AgentPendingDriversView.as_view(), name="agent-driver-pending"),
    path("agent/drivers/<int:pk>/documents/<str:doc_type>/", AgentDriverDocumentView.as_view(), name="agent-driver-document"),
    path("agent/drivers/<int:pk>/review/", AgentDriverReviewView.as_view(), name="agent-driver-review"),
    path("agent/drivers/<int:pk>/", AgentDriverDetailView.as_view(), name="agent-driver-detail"),
]

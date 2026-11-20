"""
missions/views.py
~~~~~~~~~~~~~~~~~
API endpoints only — zero business logic.
All logic is delegated to services.py.
 
Endpoint map
------------
Client:
    POST /missions/estimate/          → price estimation (no DB write)
    POST /missions/confirm/           → confirm mission (DB write)
    GET  /missions/client/my/         → list missions created by the client
 
Driver:
    GET  /missions/available/         → list active missions with status colours
    POST /missions/<id>/accept/       → accept a mission        (pending → accepted)
    POST /missions/<id>/start/        → start a mission         (accepted → in_progress)
    POST /missions/<id>/complete/     → complete a mission      (in_progress → completed)
 
Boss:
    GET  /missions/all/               → list all missions (full detail)
    GET  /missions/dashboard/         → analytics & business statistics
"""
from __future__ import annotations
 
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
  
from .models import Mission, MissionStatus
from .permissions import IsBoss, IsClient, IsDriver
from drivers.permissions import IsApprovedDriver
from .pricing import PricingError, legacy_size_for_vehicle_class
from .pricing_services import (
    build_estimate,
    create_mission_from_request,
    get_active_config,
    public_rules,
)
from .serializers import (
    BossDashboardSerializer,
    MissionBossSerializer,
    MissionClientSerializer,
    MissionCreateSerializer,
    MissionDriverListSerializer,
    PriceEstimateSerializer,
)
from .services import (
    DriverAlreadyBusyError,
    DriverNotApprovedError,
    MissionAlreadyAcceptedError,
    MissionHeldError,
    MissionInvalidTransitionError,
    MissionNotAssignedToYouError,
    accept_mission,
    assign_mission,
    cancel_mission,
    complete_mission,
    delete_mission,
    get_boss_dashboard_stats,
    start_mission,
    TRUCK_SIZE_SURCHARGE,
)
  

def _pricing_error_response(exc: PricingError) -> Response:
    """Map a pricing failure to a stable error body.

    Participant-only failures (a quote that does not belong to the caller)
    return 404 so the endpoint never leaks another client's quote.
    """
    body = {"detail": exc.message, "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    http_status = (
        status.HTTP_404_NOT_FOUND
        if exc.code == "quote_not_found"
        else status.HTTP_400_BAD_REQUEST
    )
    return Response(body, status=http_status)

 
# ---------------------------------------------------------------------------
# Client endpoints
# ---------------------------------------------------------------------------
 
class PriceEstimateView(APIView):
    """
    POST /missions/estimate/
 
    Accepts trip parameters, returns a price estimate.
    No mission is created — client can still cancel.
 
    Permissions: client only.
    """
 
    permission_classes = [IsClient]
 
    def post(self, request):
        serializer = PriceEstimateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        new_flow = data["pricing_new_flow"]

        try:
            estimate, _quote, _resolved = build_estimate(
                data,
                client=request.user,
                persist_quote=new_flow,
                enforce_bounds=new_flow,
            )
        except PricingError as exc:
            return _pricing_error_response(exc)

        if new_flow:
            components = estimate["components"]
            estimated_price = estimate["amount"]
            breakdown = {
                "distance_km": components["distance_km"],
                "workers": components["helpers_count"],
                "size": data.get("size")
                or legacy_size_for_vehicle_class(components["vehicle_class"]),
                "price_per_km": components["per_km"],
                "fixed_fuel_cost": components["base_rate"],
                "price_per_worker": components["helper_hourly"],
                "size_surcharge": 0,
            }
        else:
            # Historical response, byte-identical for old clients.
            estimated_price = data["estimated_price"]
            breakdown = {
                "distance_km": data["distance"],
                "workers": data["workers"],
                "size": data["size"],
                "price_per_km": 200,
                "fixed_fuel_cost": 2000,
                "price_per_worker": 1500,
                "size_surcharge": TRUCK_SIZE_SURCHARGE[data["size"]],
            }

        return Response(
            {
                "estimated_price": estimated_price,
                "breakdown": breakdown,
                "estimate": estimate,
            },
            status=status.HTTP_200_OK,
        )
  
  
class MissionConfirmView(APIView):
    """
    POST /missions/confirm/
  
    Client confirms the price and creates the mission.
    Price is always recalculated server-side.
  
    Permissions: client only.
    """
  
    permission_classes = [IsClient]
  
    def post(self, request):
        serializer = MissionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            mission = create_mission_from_request(
                client=request.user,
                data=serializer.validated_data,
            )
        except PricingError as exc:
            return _pricing_error_response(exc)

        return Response(
            MissionClientSerializer(mission).data,
            status=status.HTTP_201_CREATED,
        )


class PricingRulesView(APIView):
    """
    GET /pricing/rules/

    Authenticated read of the public tariff and negotiation rules.
    The commission rate is deliberately excluded.

    Permissions: any authenticated user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            public_rules(get_active_config()),
            status=status.HTTP_200_OK,
        )
 
 
# ---------------------------------------------------------------------------
# Driver endpoints
# ---------------------------------------------------------------------------
 
class AvailableMissionsView(APIView):
    """
    GET /missions/available/
 
    Returns all non-completed missions so the frontend can colour-code them:
        pending     → green   (available to accept)
        accepted    → red     (taken, shows assigned driver name)
        in_progress → orange  (being executed by assigned employee)
 
    Completed missions are excluded — they belong to history, not the dashboard.
 
    Permissions: driver only.
    """
 
    permission_classes = [IsDriver]
 
    def get(self, request):
        missions = (
            Mission.objects
            .exclude(status=MissionStatus.COMPLETED)
            .select_related("assigned_employee")
            .order_by("-created_at")
        )
        paginator = MissionPagination()
        page = paginator.paginate_queryset(missions, request, view=self)
        serializer = MissionDriverListSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)


class DriverMissionHistoryView(APIView):
    """
    GET /missions/my/
    
    Returns all missions assigned to the logged-in driver,
    including completed ones.
    """
    permission_classes = [IsDriver]

    def get(self, request):
        missions = (
            Mission.objects
            .filter(assigned_employee=request.user)
            .order_by("-created_at")
        )
        paginator = MissionPagination()
        page = paginator.paginate_queryset(missions, request, view=self)
        serializer = MissionDriverListSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)


class MissionPagination(PageNumberPagination):
    """Pagination for mission lists (50 per page)."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


class ClientMissionPagination(PageNumberPagination):
    """Small pages for a client's own mission history."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


class ClientMissionHistoryView(GenericAPIView):
    """
    GET /missions/client/my/

    Returns every mission created by the authenticated client, newest first.
    Uses the same read-only shape as the confirmation response
    (``MissionClientSerializer``). Results are paginated: 50 per page by
    default, overridable with ``?page_size=`` (max 100).

    Permissions: client only. Drivers, bosses and anonymous callers are
    rejected by ``IsClient`` (403 / 401 respectively).
    """

    permission_classes = [IsClient]
    pagination_class = ClientMissionPagination
    serializer_class = MissionClientSerializer

    def get_queryset(self):
        return (
            Mission.objects
            .filter(client=self.request.user)
            .select_related("assigned_employee")
            .order_by("-created_at")
        )

    def get(self, request):
        missions = self.get_queryset()
        page = self.paginate_queryset(missions)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(missions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AcceptMissionView(APIView):
    """
    POST /missions/<pk>/accept/
 
    Atomically assigns the requesting driver to the mission.
    Returns 409 if another driver already accepted it.

    Permissions: driver only AND profile approved (403 with code
    ``driver_not_approved`` otherwise).
    """

    permission_classes = [IsApprovedDriver]

    def post(self, request, pk: int):
        try:
            mission = accept_mission(mission_id=pk, employee=request.user)
        except Mission.DoesNotExist:
            return Response(
                {"detail": "Mission not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except MissionAlreadyAcceptedError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except MissionHeldError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "code": "mission_held",
                    "hold_expires_at": (
                        exc.hold_expires_at.isoformat()
                        if exc.hold_expires_at is not None
                        else None
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
  
        return Response(
            MissionDriverListSerializer(mission).data,
            status=status.HTTP_200_OK,
        )
  
  
class StartMissionView(APIView):
    """
    POST /missions/<pk>/start/
 
    Transitions the mission from ACCEPTED → IN_PROGRESS.
    Only the driver who accepted the mission can start it.

    Permissions: driver only AND profile approved (403 with code
    ``driver_not_approved`` otherwise).
    """

    permission_classes = [IsApprovedDriver]

    def post(self, request, pk: int):
        try:
            mission = start_mission(mission_id=pk, employee=request.user)
        except Mission.DoesNotExist:
            return Response(
                {"detail": "Mission not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except MissionNotAssignedToYouError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except MissionInvalidTransitionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except DriverAlreadyBusyError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
 
        return Response(
            MissionDriverListSerializer(mission).data,
            status=status.HTTP_200_OK,
        )
 
 
class CompleteMissionView(APIView):
    """
    POST /missions/<pk>/complete/
 
    Transitions the mission from IN_PROGRESS → COMPLETED.
    Only the driver who accepted the mission can complete it.
 
    Permissions: driver only.
    """
 
    permission_classes = [IsDriver]
 
    def post(self, request, pk: int):
        try:
            mission = complete_mission(mission_id=pk, employee=request.user)
        except Mission.DoesNotExist:
            return Response(
                {"detail": "Mission not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except MissionNotAssignedToYouError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except MissionInvalidTransitionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
 
        return Response(
            MissionDriverListSerializer(mission).data,
            status=status.HTTP_200_OK,
        )
 
 
# ---------------------------------------------------------------------------
# Boss endpoints
# ---------------------------------------------------------------------------
 
class AllMissionsView(APIView):
    """
    GET /missions/all/
 
    Returns every mission with full detail including client and
    assigned driver information.
 
    Supports optional query-param filtering:
        ?status=pending|accepted|completed
 
    Permissions: boss only.
    """
 
    permission_classes = [IsBoss]
 
    def get(self, request):
        qs = Mission.objects.select_related("client", "assigned_employee").order_by("-created_at")

        status_filter = request.query_params.get("status")
        if status_filter:
            if status_filter not in {choice[0] for choice in MissionStatus.choices}:
                return Response(
                    {"detail": "Invalid status filter.", "code": "invalid_status"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(status=status_filter)

        paginator = MissionPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = MissionBossSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AssignMissionView(APIView):
    """
    POST /missions/<pk>/assign/

    Boss-only. Assigns a specific driver to a mission.
    Body: { "employee_id": <int> }

    Permissions: boss only.
    """

    permission_classes = [IsBoss]

    def post(self, request, pk: int):
        employee_id = request.data.get("employee_id")
        if not employee_id:
            return Response(
                {"detail": "employee_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            mission = assign_mission(mission_id=pk, employee_id=employee_id)
        except Mission.DoesNotExist:
            return Response(
                {"detail": "Mission not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except MissionAlreadyAcceptedError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except DriverNotApprovedError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "code": "driver_not_approved",
                    "profile_status": exc.profile_status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            MissionBossSerializer(mission).data,
            status=status.HTTP_200_OK,
        )
 
 
class BossDashboardView(APIView):
    """
    GET /missions/dashboard/
 
    Returns aggregated business statistics for the boss:
 
        total_missions_month    — missions created in the current calendar month
        completed_missions      — completed missions this month
        completion_percentage   — completed / total * 100  (0 when no missions)
        today_missions          — missions created today
        monthly_revenue         — sum of price for completed missions this month (DZD)
        monthly_overview        — completed count per month for the last 6 months
                                  [{"month": "January", "year": 2025, "completed": 12}, ...]
 
    All computation is delegated to get_boss_dashboard_stats() in services.py.
    No business logic lives here.
 
    Permissions: boss only.
    """
 
    permission_classes = [IsBoss]
 
    def get(self, request):
        stats      = get_boss_dashboard_stats()
        serializer = BossDashboardSerializer(data=stats)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AdminDriverHistoryView(APIView):
    """
    GET /missions/employee/<id>/
    
    Returns all missions assigned to a specific driver.
    Permissions: boss only.
    """
    permission_classes = [IsBoss]

    def get(self, request, pk: int):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        if not User.objects.filter(pk=pk, role="driver").exists():
            return Response(
                {"detail": "Driver not found."}, status=status.HTTP_404_NOT_FOUND
            )

        missions = (
            Mission.objects
            .filter(assigned_employee_id=pk)
            .select_related("client", "assigned_employee")
            .order_by("-created_at")
        )
        paginator = MissionPagination()
        page = paginator.paginate_queryset(missions, request, view=self)
        serializer = MissionBossSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class CancelMissionView(APIView):
    """
    POST /missions/<pk>/cancel/
    
    Boss-only action. Cancels a mission.
    """
    permission_classes = [IsBoss]

    def post(self, request, pk: int):
        try:
            mission = cancel_mission(mission_id=pk)
            return Response(MissionBossSerializer(mission).data, status=status.HTTP_200_OK)
        except Mission.DoesNotExist:
            return Response({"detail": "Mission not found."}, status=status.HTTP_404_NOT_FOUND)
        except MissionInvalidTransitionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class MissionDeleteView(APIView):
    """
    DELETE /missions/<pk>/delete/
    
    Boss-only action. Deletes a mission permanently.
    """
    permission_classes = [IsBoss]

    def delete(self, request, pk: int):
        try:
            delete_mission(mission_id=pk)
            return Response({"message": "Mission deleted."}, status=status.HTTP_200_OK)
        except Mission.DoesNotExist:
            return Response({"detail": "Mission not found."}, status=status.HTTP_404_NOT_FOUND)

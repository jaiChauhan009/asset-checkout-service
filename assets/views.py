from django.db import connection
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.utils import timezone
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Asset, CheckOut, Employee
from .serializers import (
    AssetDetailSerializer,
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutSerializer,
    OverdueRowSerializer,
    ReturnSerializer,
)


class AssetViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Asset.objects.all()
    filterset_fields = ["status", "category"]
    search_fields = ["name", "asset_tag"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AssetDetailSerializer
        return AssetSerializer


class CheckOutViewSet(viewsets.GenericViewSet):
    queryset = CheckOut.objects.select_related("asset", "employee")
    serializer_class = CheckOutSerializer

    def create(self, request):
        data = CheckOutCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        checkout = services.check_out(**data.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="return")
    def return_item(self, request, pk=None):
        data = ReturnSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        checkout = services.return_checkout(checkout_id=pk, **data.validated_data)
        checkout = self.get_queryset().get(pk=checkout.pk)
        return Response(CheckOutSerializer(checkout).data)


class EmployeeSummaryView(APIView):
    def get(self, request, employee_code):
        now = timezone.now()
        open_q = Q(checkouts__returned_at__isnull=True)
        hold = ExpressionWrapper(
            F("checkouts__returned_at") - F("checkouts__checked_out_at"),
            output_field=DurationField(),
        )
        # One SQL query: GROUP BY employee with four filtered aggregates.
        row = (
            Employee.objects.filter(employee_code=employee_code)
            .annotate(
                lifetime=Count("checkouts"),
                held=Count("checkouts", filter=open_q),
                overdue=Count("checkouts", filter=open_q & Q(checkouts__due_at__lt=now)),
                mean_hold=Avg(hold, filter=Q(checkouts__returned_at__isnull=False)),
            )
            .values("employee_code", "lifetime", "held", "overdue", "mean_hold")
            .first()
        )
        if row is None:
            raise NotFound(f"Employee '{employee_code}' not found.")
        mean = row["mean_hold"]
        return Response({
            "employee_code": row["employee_code"],
            "lifetime_checkouts": row["lifetime"],
            "currently_held": row["held"],
            "currently_overdue": row["overdue"],
            "mean_hold_days": round(mean.total_seconds() / 86400, 2) if mean else None,
        })


class OverdueReportView(generics.ListAPIView):
    serializer_class = OverdueRowSerializer

    def get_queryset(self):
        # select_related -> one JOINed query (plus pagination COUNT), not one per row.
        return (
            CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=self._now)
            .select_related("asset", "employee")
            .order_by("due_at", "id")
        )

    def list(self, request, *args, **kwargs):
        self._now = timezone.now()
        return super().list(request, *args, **kwargs)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": self._now}


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return Response({"status": "ok", "database": "ok"})
    except Exception as exc:  # report, don't crash
        return Response(
            {"status": "error", "database": str(exc)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

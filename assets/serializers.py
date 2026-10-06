from django.utils import timezone
from rest_framework import serializers

from .models import Asset, CheckOut


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = [
            "id", "asset_tag", "name", "category", "status",
            "purchase_date", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class AssetDetailSerializer(AssetSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta(AssetSerializer.Meta):
        fields = AssetSerializer.Meta.fields + ["current_holder"]

    def get_current_holder(self, asset):
        if asset.status != Asset.Status.CHECKED_OUT:
            return None
        open_co = (
            asset.checkouts.filter(returned_at__isnull=True)
            .select_related("employee")
            .first()
        )
        if open_co is None:
            return None
        return {
            "employee_code": open_co.employee.employee_code,
            "full_name": open_co.employee.full_name,
        }


class CheckOutSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    employee_code = serializers.CharField(
        source="employee.employee_code", read_only=True
    )

    class Meta:
        model = CheckOut
        fields = [
            "id", "asset_tag", "employee_code", "checked_out_at",
            "due_at", "returned_at", "condition_note",
        ]


class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()


class ReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(required=False, allow_blank=True, default="")
    needs_maintenance = serializers.BooleanField(required=False, default=False)


class OverdueRowSerializer(serializers.ModelSerializer):
    asset_name = serializers.CharField(source="asset.name")
    asset_tag = serializers.CharField(source="asset.asset_tag")
    employee_code = serializers.CharField(source="employee.employee_code")
    employee_name = serializers.CharField(source="employee.full_name")
    days_overdue = serializers.SerializerMethodField()

    class Meta:
        model = CheckOut
        fields = [
            "id", "asset_name", "asset_tag", "employee_code",
            "employee_name", "due_at", "days_overdue",
        ]

    def get_days_overdue(self, checkout):
        now = self.context.get("now") or timezone.now()
        return (now - checkout.due_at).days

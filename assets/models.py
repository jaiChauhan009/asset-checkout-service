from django.db import models
from django.db.models import Q


class Asset(models.Model):
    class Category(models.TextChoices):
        CAMERA = "CAMERA"
        LAPTOP = "LAPTOP"
        SENSOR = "SENSOR"
        VEHICLE = "VEHICLE"

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE"
        CHECKED_OUT = "CHECKED_OUT"
        MAINTENANCE = "MAINTENANCE"

    asset_tag = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=16, choices=Category.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.AVAILABLE
    )
    purchase_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["asset_tag"]

    def __str__(self):
        return f"{self.asset_tag} ({self.name})"


class Employee(models.Model):
    employee_code = models.CharField(max_length=16, unique=True, db_index=True)
    full_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["employee_code"]

    def __str__(self):
        return f"{self.employee_code} ({self.full_name})"


class CheckOut(models.Model):
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="checkouts"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="checkouts"
    )
    checked_out_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)
    condition_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-checked_out_at"]
        constraints = [
            # Database-level backstop for rule 7: an asset can have only one
            # open check-out, whatever the application code does.
            models.UniqueConstraint(
                fields=["asset"],
                condition=Q(returned_at__isnull=True),
                name="one_open_checkout_per_asset",
            ),
        ]

    def __str__(self):
        return f"CheckOut #{self.pk} {self.asset_id} -> {self.employee_id}"


class OverdueNotice(models.Model):
    checkout = models.ForeignKey(
        CheckOut, on_delete=models.CASCADE, related_name="notices"
    )
    notice_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["checkout", "notice_date"],
                name="one_notice_per_checkout_per_day",
            ),
        ]

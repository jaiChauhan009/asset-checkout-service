from django.contrib import admin
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from assets import views

router = DefaultRouter(trailing_slash=True)
router.register("assets", views.AssetViewSet)
router.register("checkouts", views.CheckOutViewSet)

api = [
    path("", include(router.urls)),
    path("auth/token/", obtain_auth_token),
    path("employees/<str:employee_code>/summary/", views.EmployeeSummaryView.as_view()),
    path("reports/overdue/", views.OverdueReportView.as_view()),
    path("health/", views.health),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api)),
]

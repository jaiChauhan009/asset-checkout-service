from django.contrib import admin

from .models import Asset, CheckOut, Employee, OverdueNotice

admin.site.register([Asset, Employee, CheckOut, OverdueNotice])

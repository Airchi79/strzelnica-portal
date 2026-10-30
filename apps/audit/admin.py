from django.contrib import admin

from .models import AuditLog, SensitiveAccessLog


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ["at", "actor", "action", "summary", "ip"]
    search_fields = ["action", "summary"]


@admin.register(SensitiveAccessLog)
class SensitiveAccessLogAdmin(ReadOnlyAdmin):
    list_display = ["at", "actor", "subject", "resource", "ip"]

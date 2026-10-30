from django.contrib import admin

from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["email", "first_name", "last_name", "is_active", "is_staff", "is_superuser", "email_verified_at"]
    search_fields = ["email", "first_name", "last_name"]
    list_filter = ["is_active", "is_staff", "is_superuser"]
    exclude = ["password"]
    readonly_fields = ["last_login", "date_joined"]

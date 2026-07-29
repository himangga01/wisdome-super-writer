from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .forms import AdminAccountChangeForm, AdminAccountCreationForm
from .models import AdminAccount, ReauthenticationProof


@admin.register(AdminAccount)
class AdminAccountAdmin(UserAdmin):
    add_form = AdminAccountCreationForm
    form = AdminAccountChangeForm
    model = AdminAccount
    ordering = ("email",)
    list_display = ("email", "is_staff", "is_active", "last_login")
    search_fields = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        (
            "Timestamps",
            {
                "fields": (
                    "last_login",
                    "last_reauthenticated_at",
                    "reauth_failure_count",
                    "reauth_failure_window_started_at",
                    "reauth_locked_until",
                )
            },
        ),
    )
    add_fieldsets = ((None, {"fields": ("email", "password1", "password2")}),)
    readonly_fields = (
        "last_login",
        "last_reauthenticated_at",
        "reauth_failure_count",
        "reauth_failure_window_started_at",
        "reauth_locked_until",
    )


@admin.register(ReauthenticationProof)
class ReauthenticationProofAdmin(admin.ModelAdmin):
    list_display = ("id", "admin", "state", "issued_at", "expires_at", "consumed_action")
    list_filter = ("state", "consumed_action")
    readonly_fields = tuple(field.name for field in ReauthenticationProof._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


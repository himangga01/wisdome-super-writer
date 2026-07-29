from django.contrib import admin

from .models import (
    CollectionRun,
    RunSourceItem,
    RunStep,
    SourceCollectionAttempt,
    SourceItem,
)


class CollectionReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(
    (
        CollectionRun,
        SourceCollectionAttempt,
        SourceItem,
        RunSourceItem,
        RunStep,
    ),
    CollectionReadOnlyAdmin,
)

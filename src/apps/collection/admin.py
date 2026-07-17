from django.contrib import admin

from .models import CollectionRun, RunSourceItem, RunStep, SourceCollectionAttempt, SourceItem

admin.site.register([CollectionRun, SourceCollectionAttempt, SourceItem, RunSourceItem, RunStep])

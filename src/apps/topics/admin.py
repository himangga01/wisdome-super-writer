from django.contrib import admin

from .models import SourceDefinition, SourceDefinitionSnapshot, SourceRegistryMembership, SourceRegistrySnapshot, TopicPolicy

admin.site.register([TopicPolicy, SourceDefinition, SourceDefinitionSnapshot, SourceRegistrySnapshot, SourceRegistryMembership])

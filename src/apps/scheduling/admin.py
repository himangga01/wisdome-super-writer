from django.contrib import admin

from .models import KillSwitchDecision, OperationalControl, Schedule, ScheduleDispatch

admin.site.register([Schedule, ScheduleDispatch, OperationalControl, KillSwitchDecision])

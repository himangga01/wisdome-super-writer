from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def console_runs(request):
    return render(request, "admin_console/runs.html")

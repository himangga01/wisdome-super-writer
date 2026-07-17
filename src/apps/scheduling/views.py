from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def console_operations(request):
    return render(request, "admin_console/operations/index.html")

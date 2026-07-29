from django.contrib.admin.views.decorators import staff_member_required
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.views.decorators.csrf import ensure_csrf_cookie
from django.shortcuts import render


def _console_view(template_name: str, *, fallback: str | None = None):
    @staff_member_required(login_url="/admin/login/")
    @ensure_csrf_cookie
    def view(request):
        selected_template = template_name
        try:
            get_template(selected_template)
        except TemplateDoesNotExist:
            if fallback is None:
                raise
            selected_template = fallback
        return render(request, selected_template)

    return view


home = _console_view("admin_console/index.html")
runs = _console_view("admin_console/runs.html")
articles = _console_view("admin_console/articles.html")
sources = _console_view("admin_console/sources/index.html")
publishing = _console_view(
    "admin_console/publishing/index.html",
    fallback="admin_console/index.html",
)
operations = _console_view("admin_console/operations/index.html")


@staff_member_required(login_url="/admin/login/")
@ensure_csrf_cookie
def run_detail(request, run_id):
    return render(request, "admin_console/run_detail.html", {"run_id": run_id})

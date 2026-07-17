from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import AdminAccount


class AdminAccountCreationForm(UserCreationForm):
    class Meta:
        model = AdminAccount
        fields = ("email",)


class AdminAccountChangeForm(UserChangeForm):
    class Meta:
        model = AdminAccount
        fields = ("email", "is_active", "is_staff", "is_superuser")


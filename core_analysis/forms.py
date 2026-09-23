from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from core_analysis.models import AccountApproval


class AdminApprovalRegistrationForm(UserCreationForm):
    email = forms.EmailField(
        label="Email address",
        help_text="Admins use this email to review your access request.",
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "placeholder": "you@example.com",
            }
        ),
    )
    field_order = ["username", "email", "password1", "password2"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        UserModel = get_user_model()
        if UserModel._default_manager.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email address is already registered.")
        if AccountApproval.objects.filter(contact_email__iexact=email).exists():
            raise forms.ValidationError("This email address is already registered.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_active = False
        if commit:
            user.save()
        return user

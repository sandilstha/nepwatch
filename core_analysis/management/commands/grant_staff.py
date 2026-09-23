"""
grant_staff — give (or revoke) a My Portfolio account access to the Workbench.

The Workbench is staff-restricted but now shares the My Portfolio login page, so
the same username/password works for both once the account is marked staff:

    python manage.py grant_staff <username>            # grant Workbench access
    python manage.py grant_staff <username> --revoke   # remove it
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Grant or revoke Workbench (staff) access for a My Portfolio user."

    def add_arguments(self, parser):
        parser.add_argument("username", help="The My Portfolio account username.")
        parser.add_argument(
            "--revoke", action="store_true",
            help="Remove Workbench access instead of granting it.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(
                f"No user named {username!r}. Register at /accounts/register/ first, "
                "then re-run this command."
            )
        user.is_staff = not options["revoke"]
        user.save(update_fields=["is_staff"])
        verb = "revoked" if options["revoke"] else "granted"
        self.stdout.write(self.style.SUCCESS(
            f"Workbench access {verb} for {user.username}. "
            f"They can now use the same My Portfolio login at /workbench/."
            if not options["revoke"] else
            f"Workbench access {verb} for {user.username}."
        ))

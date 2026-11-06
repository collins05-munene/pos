from django.db import models

from tenants.models import TenantOwnedModel


class OnboardingProgress(TenantOwnedModel):
    """
    One row per shop remembering what the owner chose to do with the setup guide.

    What is actually DONE is never stored: it is worked out from the shop's real data (does it have a
    product yet? any stock?) in onboarding/services.py, so the guide can't drift out of sync with reality.
    This row only keeps the choices the data can't tell us.
    """
    skipped_steps = models.JSONField(default=list, blank=True,
                                     help_text="Optional steps the owner chose to do later, e.g. ['brand'].")
    welcomed_at = models.DateTimeField(null=True, blank=True,
                                       help_text="The one-time redirect to the guide has already happened.")
    dismissed_at = models.DateTimeField(null=True, blank=True,
                                        help_text="The owner hid the dashboard reminder.")
    completed_at = models.DateTimeField(null=True, blank=True,
                                        help_text="Every step was finished. Stops all further checks.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant"], name="uniq_onboarding_per_tenant")]

    def __str__(self):
        return f"Onboarding for tenant {self.tenant_id}"

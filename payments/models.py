from django.db import models

from tenants.models import TenantOwnedModel


class MpesaTransaction(TenantOwnedModel):
    # Safaricom's ids are globally unique, so this stays globally unique. NOTE: the
    # callback is an anonymous request from Safaricom (no user, no tenant), so the callback
    # view must look the row up with MpesaTransaction.all_objects and then run the rest of
    # its work inside `with tenant_context(txn.tenant):`.
    checkout_request_id = models.CharField(max_length=100, unique=True)
    merchant_request_id = models.CharField(max_length=100)
    order = models.OneToOneField('sales.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='mpesa_transaction')
    phone_number = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default='PENDING')
    result_description = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Number: {self.phone_number} Amount: {self.amount} Status: {self.status}"

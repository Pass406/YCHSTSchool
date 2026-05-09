from django.urls import path
from . import webhooks

app_name = "payments"

urlpatterns = [
    path("webhook/paystack/", webhooks.paystack_webhook, name="paystack_webhook"),
]

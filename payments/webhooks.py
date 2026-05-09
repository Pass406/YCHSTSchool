"""
Paystack webhook handler.

Paystack sends signed POST requests to this endpoint whenever a payment
event occurs (charge.success, transfer.success, etc.).  We verify the
HMAC-SHA512 signature using PAYSTACK_SECRET_KEY, then update the relevant
payment record automatically — so payments confirmed server-side (e.g. via
USSD or bank transfer) are reflected without the student having to click
"verify".

Webhook URL to register in your Paystack dashboard:
    https://<your-domain>/payments/webhook/paystack/

Django URL conf entry (added in sanga_portal/urls.py):
    path('payments/', include('payments.urls'))
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


def _verify_paystack_signature(request) -> bool:
    """
    Validate the X-Paystack-Signature header against the raw request body.

    Paystack signs the payload with HMAC-SHA512 using your secret key.
    Returns True only when the computed digest matches the header value.
    """
    secret_key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret_key:
        logger.error(
            "PAYSTACK_SECRET_KEY is not set — cannot validate webhook signature."
        )
        return False

    paystack_signature = request.headers.get("X-Paystack-Signature", "")
    if not paystack_signature:
        logger.warning("Webhook received without X-Paystack-Signature header.")
        return False

    computed = hmac.new(
        secret_key.encode("utf-8"),
        msg=request.body,
        digestmod=hashlib.sha512,
    ).hexdigest()

    return hmac.compare_digest(computed, paystack_signature)


def _handle_charge_success(data: dict) -> None:
    """
    Process a charge.success event.

    Looks up the transaction reference in both FeePayment and
    HostelAllocation and marks them as paid/completed.
    """
    reference = data.get("reference", "")
    amount_kobo = data.get("amount", 0)
    from decimal import Decimal
    amount_naira = Decimal(str(amount_kobo)) / 100

    logger.info(
        "charge.success webhook: ref=%s amount=₦%s", reference, amount_naira
    )

    # ── Fee payments ──────────────────────────────────────────────────────
    try:
        from fees.models import FeePayment
        payment = FeePayment.objects.get(transaction_reference=reference)
        if payment.status != "completed":
            payment.status = "completed"
            payment.verified_at = timezone.now()
            payment.gateway_response = data
            payment.save(update_fields=["status", "verified_at", "gateway_response"])

            # Update student's total fees paid
            try:
                profile = payment.student.student_profile
                profile.total_fees_paid += payment.amount
                profile.save(update_fields=["total_fees_paid"])
            except Exception:
                pass

            logger.info(
                "FeePayment %s marked completed via webhook (ref=%s)",
                payment.pk,
                reference,
            )
    except FeePayment.DoesNotExist:
        pass  # Not a fee payment — check hostel next
    except Exception as exc:
        logger.exception(
            "Error updating FeePayment for ref=%s: %s", reference, exc
        )

    # ── Hostel allocations ────────────────────────────────────────────────
    try:
        from hostels.models import HostelAllocation
        allocation = HostelAllocation.objects.get(payment_reference=reference)
        if allocation.status != "paid":
            allocation.status = "paid"
            allocation.payment_date = timezone.now()
            allocation.gateway_response = data
            allocation.save(
                update_fields=["status", "payment_date", "gateway_response"]
            )
            logger.info(
                "HostelAllocation %s marked paid via webhook (ref=%s)",
                allocation.pk,
                reference,
            )
    except HostelAllocation.DoesNotExist:
        pass
    except Exception as exc:
        logger.exception(
            "Error updating HostelAllocation for ref=%s: %s", reference, exc
        )


@csrf_exempt
@require_POST
def paystack_webhook(request):
    """
    Endpoint: POST /payments/webhook/paystack/

    Receives and processes Paystack event notifications.
    Always returns HTTP 200 to Paystack (even on handled errors) so that
    Paystack does not keep retrying for non-transient issues.
    """
    # 1. Verify signature
    if not _verify_paystack_signature(request):
        logger.warning(
            "Paystack webhook rejected — invalid signature. "
            "IP: %s", request.META.get("REMOTE_ADDR")
        )
        return HttpResponseForbidden("Invalid signature.")

    # 2. Parse body
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        logger.error("Paystack webhook received non-JSON body.")
        return HttpResponseBadRequest("Invalid JSON.")

    event = payload.get("event", "")
    data = payload.get("data", {})

    logger.info("Paystack webhook received: event=%s", event)

    # 3. Dispatch by event type
    if event == "charge.success":
        _handle_charge_success(data)
    else:
        # Log unhandled events but still return 200 so Paystack stops retrying
        logger.debug("Unhandled Paystack event: %s", event)

    return HttpResponse(status=200)

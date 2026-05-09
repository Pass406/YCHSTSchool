"""
Paystack payment gateway integration.

All communication with the Paystack API is centralised here so that
fees/views.py, hostels/views.py, and the webhook handler share a single,
testable interface.

Environment variables required (set in .env / Railway variables):
    PAYSTACK_SECRET_KEY  – sk_live_… or sk_test_… key from Paystack dashboard
    PAYSTACK_PUBLIC_KEY  – pk_live_… or pk_test_… key (exposed to frontend)

Paystack API reference: https://paystack.com/docs/api/
"""

import logging
import uuid
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

PAYSTACK_BASE_URL = "https://api.paystack.co"


class PaystackError(Exception):
    """Raised when the Paystack API returns an error or is unreachable."""
    pass


class PaystackGateway:
    """
    Thin wrapper around the Paystack REST API.

    Usage::

        gw = PaystackGateway()
        result = gw.initialize_payment(
            amount=Decimal("5000.00"),
            email="student@example.com",
            reference="TXN-ABC123",
            metadata={"student_id": 42, "purpose": "tuition_fee"},
        )
        # result["authorization_url"] → redirect the user here
        # result["reference"]         → store this for later verification

        status = gw.verify_payment("TXN-ABC123")
        # status["paid"]      → True / False
        # status["amount"]    → Decimal amount in Naira
        # status["raw"]       → full Paystack data dict for storage
    """

    def __init__(self):
        self.secret_key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
        if not self.secret_key:
            logger.warning(
                "PAYSTACK_SECRET_KEY is not configured. "
                "Payment gateway calls will fail."
            )

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f"{PAYSTACK_BASE_URL}{endpoint}"
        try:
            response = requests.post(
                url, json=payload, headers=self._headers(), timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            logger.error("Paystack API timed out for POST %s", endpoint)
            raise PaystackError("Payment gateway timed out. Please try again.")
        except requests.exceptions.ConnectionError:
            logger.error("Could not connect to Paystack API for POST %s", endpoint)
            raise PaystackError(
                "Could not reach payment gateway. Check your internet connection."
            )
        except requests.exceptions.HTTPError as exc:
            body = {}
            try:
                body = exc.response.json()
            except Exception:
                pass
            message = body.get("message", str(exc))
            logger.error(
                "Paystack HTTP error %s for POST %s: %s",
                exc.response.status_code,
                endpoint,
                message,
            )
            raise PaystackError(f"Payment gateway error: {message}")

    def _get(self, endpoint: str) -> dict:
        url = f"{PAYSTACK_BASE_URL}{endpoint}"
        try:
            response = requests.get(
                url, headers=self._headers(), timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            logger.error("Paystack API timed out for GET %s", endpoint)
            raise PaystackError("Payment gateway timed out. Please try again.")
        except requests.exceptions.ConnectionError:
            logger.error("Could not connect to Paystack API for GET %s", endpoint)
            raise PaystackError(
                "Could not reach payment gateway. Check your internet connection."
            )
        except requests.exceptions.HTTPError as exc:
            body = {}
            try:
                body = exc.response.json()
            except Exception:
                pass
            message = body.get("message", str(exc))
            logger.error(
                "Paystack HTTP error %s for GET %s: %s",
                exc.response.status_code,
                endpoint,
                message,
            )
            raise PaystackError(f"Payment gateway error: {message}")

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def initialize_payment(
        self,
        amount: Decimal,
        email: str,
        reference: str = None,
        callback_url: str = None,
        metadata: dict = None,
    ) -> dict:
        """
        Create a new Paystack transaction and return the checkout URL.

        Args:
            amount:       Amount in **Naira** (will be converted to kobo).
            email:        Customer's email address.
            reference:    Unique transaction reference. Auto-generated if omitted.
            callback_url: URL Paystack redirects to after payment.
            metadata:     Arbitrary dict stored against the transaction.

        Returns:
            {
                "authorization_url": "https://checkout.paystack.com/…",
                "access_code":       "…",
                "reference":         "TXN-…",
            }

        Raises:
            PaystackError: on any API or network failure.
        """
        if not reference:
            reference = f"TXN-{uuid.uuid4().hex[:16].upper()}"

        # Paystack expects amount in kobo (1 Naira = 100 kobo)
        amount_kobo = int(Decimal(str(amount)) * 100)

        payload = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "currency": "NGN",
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if metadata:
            payload["metadata"] = metadata

        logger.info(
            "Initialising Paystack payment: ref=%s amount_kobo=%d email=%s",
            reference,
            amount_kobo,
            email,
        )

        data = self._post("/transaction/initialize", payload)

        if not data.get("status"):
            raise PaystackError(
                data.get("message", "Failed to initialise payment.")
            )

        return {
            "authorization_url": data["data"]["authorization_url"],
            "access_code": data["data"]["access_code"],
            "reference": data["data"]["reference"],
        }

    def verify_payment(self, reference: str) -> dict:
        """
        Verify a transaction with Paystack and return a normalised result.

        Args:
            reference: The transaction reference to verify.

        Returns:
            {
                "paid":     True if status == "success",
                "status":   raw Paystack status string,
                "amount":   Decimal amount in Naira,
                "currency": "NGN",
                "channel":  payment channel (card, bank, ussd, …),
                "email":    customer email,
                "raw":      full Paystack data dict (store in gateway_response),
            }

        Raises:
            PaystackError: on any API or network failure.
        """
        logger.info("Verifying Paystack payment: ref=%s", reference)

        data = self._get(f"/transaction/verify/{reference}")

        if not data.get("status"):
            raise PaystackError(
                data.get("message", "Failed to verify payment.")
            )

        tx = data["data"]
        amount_naira = Decimal(str(tx.get("amount", 0))) / 100

        return {
            "paid": tx.get("status") == "success",
            "status": tx.get("status", "unknown"),
            "amount": amount_naira,
            "currency": tx.get("currency", "NGN"),
            "channel": tx.get("channel", ""),
            "email": tx.get("customer", {}).get("email", ""),
            "raw": tx,
        }

    def get_payment_status(self, reference: str) -> str:
        """
        Lightweight helper that returns just the Paystack status string
        (e.g. "success", "failed", "abandoned") for a given reference.

        Raises:
            PaystackError: on any API or network failure.
        """
        result = self.verify_payment(reference)
        return result["status"]

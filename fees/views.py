import logging
import uuid

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.models import StudentProfile
from accounts.matric_utils import generate_matriculation_number
from payments.gateway import PaystackGateway, PaystackError

from .models import FeeStructure, FeePayment, OtherPayment
from .serializers import (
    FeePaymentSerializer,
    FeeStructureSerializer,
    OtherPaymentSerializer,
)

logger = logging.getLogger(__name__)


class FeeStructureListView(generics.ListAPIView):
    """List fee structures"""
    serializer_class = FeeStructureSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return FeeStructure.objects.filter(is_active=True)


class FeePaymentListCreateView(generics.ListCreateAPIView):
    """List and create fee payments"""
    serializer_class = FeePaymentSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return FeePayment.objects.filter(student=self.request.user)
    
    def perform_create(self, serializer):
        serializer.save(student=self.request.user)


class FeePaymentDetailView(generics.RetrieveAPIView):
    """Retrieve fee payment details"""
    serializer_class = FeePaymentSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return FeePayment.objects.filter(student=self.request.user)


class OtherPaymentListCreateView(generics.ListCreateAPIView):
    """List and create other payments"""
    serializer_class = OtherPaymentSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return OtherPayment.objects.filter(student=self.request.user)
    
    def perform_create(self, serializer):
        serializer.save(student=self.request.user)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def student_fees_dashboard(request):
    """Student fees dashboard"""
    user = request.user
    
    try:
        profile = user.student_profile
    except:
        return Response({'error': 'Student profile not found'}, status=400)
    
    # Get current fee structure
    try:
        fee_structure = FeeStructure.objects.get(
            level=profile.level,
            program=profile.department_code,
            academic_year='2023/2024',  # This should be configurable
            is_active=True
        )
    except FeeStructure.DoesNotExist:
        return Response({'error': 'Fee structure not found for your level/program'}, status=404)
    
    # Get total payments made
    total_paid = FeePayment.objects.filter(
        student=user,
        status='completed'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    # Get payment history
    payment_history = FeePayment.objects.filter(
        student=user
    ).order_by('-payment_date')[:10]
    
    # Calculate outstanding balance
    outstanding_balance = fee_structure.total_fee - total_paid
    
    data = {
        'fee_structure': FeeStructureSerializer(fee_structure).data,
        'payment_summary': {
            'total_fee': fee_structure.total_fee,
            'total_paid': total_paid,
            'outstanding_balance': outstanding_balance,
            'payment_percentage': (total_paid / fee_structure.total_fee * 100) if fee_structure.total_fee > 0 else 0,
        },
        'payment_history': FeePaymentSerializer(payment_history, many=True).data,
    }
    
    return Response(data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def initiate_online_payment(request):
    """
    Initiate an online fee payment via Paystack.

    Expected request body:
        {
            "amount":       "5000.00",   // Naira
            "purpose":      "tuition_fee",
            "callback_url": "https://…"  // optional — where Paystack redirects after payment
        }

    Returns:
        {
            "payment_id":          <int>,
            "transaction_reference": "TXN-…",
            "authorization_url":   "https://checkout.paystack.com/…",
            "amount":              "5000.00",
            "status":              "pending"
        }
    """
    user = request.user
    amount = request.data.get('amount')
    purpose = request.data.get('purpose', 'tuition_fee')
    callback_url = request.data.get('callback_url', '')

    if not amount:
        return Response(
            {'error': 'Amount is required'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Require a valid email — Paystack mandates it
    email = user.email
    if not email:
        return Response(
            {'error': 'Your account does not have an email address. '
                      'Please contact the bursary to update your record.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Generate a unique transaction reference
    transaction_ref = f"TXN-{uuid.uuid4().hex[:16].upper()}"

    # Persist a pending payment record before hitting the gateway so we
    # always have a local record even if the redirect is never completed.
    payment = FeePayment.objects.create(
        student=user,
        amount=amount,
        payment_method='online',
        transaction_reference=transaction_ref,
        purpose=purpose,
        status='pending',
        payment_gateway='paystack',
    )

    # Call Paystack to get the checkout URL
    try:
        gateway = PaystackGateway()
        result = gateway.initialize_payment(
            amount=payment.amount,
            email=email,
            reference=transaction_ref,
            callback_url=callback_url or None,
            metadata={
                'payment_id': payment.pk,
                'student_id': user.pk,
                'purpose': purpose,
            },
        )
    except PaystackError as exc:
        # Mark the payment as failed so the student can retry cleanly
        payment.status = 'failed'
        payment.save(update_fields=['status'])
        logger.error(
            "Paystack initialisation failed for user %s ref %s: %s",
            user.username, transaction_ref, exc,
        )
        return Response(
            {'error': str(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    logger.info(
        "Payment initiated: user=%s ref=%s amount=%s",
        user.username, transaction_ref, amount,
    )

    return Response({
        'payment_id': payment.id,
        'transaction_reference': transaction_ref,
        'authorization_url': result['authorization_url'],
        'amount': str(payment.amount),
        'status': 'pending',
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def verify_payment(request):
    """
    Verify a fee payment against the Paystack API.

    Expected request body:
        {"transaction_reference": "TXN-…"}

    The view calls Paystack's /transaction/verify endpoint and only marks
    the payment as completed when Paystack confirms the charge succeeded.
    If the payment is already completed (e.g. confirmed via webhook earlier)
    the stored status is returned immediately without a redundant API call.
    """
    transaction_ref = request.data.get('transaction_reference')

    if not transaction_ref:
        return Response(
            {'error': 'Transaction reference is required'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        payment = FeePayment.objects.get(transaction_reference=transaction_ref)
    except FeePayment.DoesNotExist:
        return Response(
            {'error': 'Payment not found'},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Ownership check — students may only verify their own payments
    if payment.student != request.user and not request.user.is_staff:
        return Response(
            {'error': 'You do not have permission to verify this payment.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Already confirmed (possibly via webhook) — return current state
    if payment.status == 'completed':
        return Response({
            'payment_id': payment.id,
            'status': payment.status,
            'amount': str(payment.amount),
            'verified_at': payment.verified_at,
            'message': 'Payment already verified.',
        })

    # Verify with Paystack
    try:
        gateway = PaystackGateway()
        result = gateway.verify_payment(transaction_ref)
    except PaystackError as exc:
        logger.error(
            "Paystack verification failed for ref %s: %s", transaction_ref, exc
        )
        return Response(
            {'error': str(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if result['paid']:
        payment.status = 'completed'
        payment.verified_by = request.user
        payment.verified_at = timezone.now()
        payment.gateway_response = result['raw']
        payment.save(
            update_fields=['status', 'verified_by', 'verified_at', 'gateway_response']
        )

        # Update student's cumulative fees paid
        try:
            profile = payment.student.student_profile
            profile.total_fees_paid += payment.amount
            profile.save(update_fields=['total_fees_paid'])
        except StudentProfile.DoesNotExist:
            logger.warning(
                "No StudentProfile found for %s during payment verification",
                payment.student.username,
            )

        logger.info(
            "FeePayment %s verified successfully via Paystack (ref=%s, user=%s)",
            payment.pk, transaction_ref, request.user.username,
        )
    else:
        # Paystack returned a non-success status (failed, abandoned, etc.)
        payment.status = 'failed'
        payment.gateway_response = result['raw']
        payment.save(update_fields=['status', 'gateway_response'])
        logger.warning(
            "Paystack verification returned status '%s' for ref %s",
            result['status'], transaction_ref,
        )

    return Response({
        'payment_id': payment.id,
        'status': payment.status,
        'amount': str(payment.amount),
        'verified_at': payment.verified_at,
        'gateway_status': result['status'],
    })
"""
Hostel views — fully template-based Django views (not DRF).
Covers the student hostel dashboard, bed application, and admin management.
"""

import logging
import uuid

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse

from payments.gateway import PaystackGateway, PaystackError

from .models import Hostel, Room, BedSpace, HostelAllocation

logger = logging.getLogger(__name__)



# ─────────────────────────────────────────────────────────────
#  STUDENT VIEWS
# ─────────────────────────────────────────────────────────────

@login_required
def student_hostel_dashboard(request):
    """
    Main hostel page for students:
    - Shows their current allocation (if any)
    - Lists all available hostels with room/bed counts
    - Allows application for an available bed
    """
    user = request.user

    # Current active allocation
    allocation = HostelAllocation.objects.filter(
        student=user,
        status__in=['pending_payment', 'paid'],
    ).select_related('bed_space__room__hostel').first()

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'generate_invoice' and allocation and allocation.status == 'pending_payment':
            # Generate a Paystack transaction reference and initialise the payment
            ref = f"HST-{uuid.uuid4().hex[:12].upper()}"
            email = user.email

            if not email:
                messages.error(
                    request,
                    'Your account does not have an email address. '
                    'Please contact the hostel office to update your record.',
                )
                return redirect('hostels:student_dashboard')

            hostel_price = allocation.bed_space.room.hostel.price_per_session

            try:
                gateway = PaystackGateway()
                result = gateway.initialize_payment(
                    amount=hostel_price,
                    email=email,
                    reference=ref,
                    metadata={
                        'allocation_id': allocation.pk,
                        'student_id': user.pk,
                        'purpose': 'hostel_fee',
                    },
                )
                allocation.payment_reference = ref
                allocation.payment_gateway = 'paystack'
                allocation.save(update_fields=['payment_reference', 'payment_gateway'])
                messages.success(
                    request,
                    f'Hostel invoice generated (ref: {ref}). '
                    'Proceed to payment using the link sent to your email or '
                    'click "Pay Now" to complete your payment.',
                )
                # Store the authorization URL in the session so the template
                # can offer a direct "Pay Now" button.
                request.session['hostel_payment_url'] = result['authorization_url']
            except PaystackError as exc:
                logger.error(
                    "Paystack invoice generation failed for allocation %s: %s",
                    allocation.pk, exc,
                )
                messages.error(
                    request,
                    f'Could not generate payment invoice: {exc}. Please try again.',
                )
            return redirect('hostels:student_dashboard')

        elif action == 'verify_payment' and allocation and allocation.status == 'pending_payment' and allocation.payment_reference:
            # Verify the payment with Paystack before marking as paid
            try:
                gateway = PaystackGateway()
                result = gateway.verify_payment(allocation.payment_reference)
            except PaystackError as exc:
                logger.error(
                    "Paystack verification failed for allocation %s ref %s: %s",
                    allocation.pk, allocation.payment_reference, exc,
                )
                messages.error(
                    request,
                    f'Payment verification failed: {exc}. Please try again or contact support.',
                )
                return redirect('hostels:student_dashboard')

            if result['paid']:
                allocation.status = 'paid'
                allocation.payment_date = timezone.now()
                allocation.gateway_response = result['raw']
                allocation.save(update_fields=['status', 'payment_date', 'gateway_response'])
                logger.info(
                    "HostelAllocation %s marked paid via student verification (ref=%s)",
                    allocation.pk, allocation.payment_reference,
                )
                messages.success(
                    request,
                    'Hostel payment verified successfully! Your bed space is now secured.',
                )
            else:
                messages.warning(
                    request,
                    f'Payment not confirmed by gateway (status: {result["status"]}). '
                    'If you have completed payment, please wait a few minutes and try again.',
                )
            return redirect('hostels:student_dashboard')



    # All hostels with their rooms
    hostels = Hostel.objects.prefetch_related('rooms__bed_spaces').all()

    # Annotate available bed count per hostel
    hostel_data = []
    for hostel in hostels:
        avail = BedSpace.objects.filter(
            room__hostel=hostel,
            is_available=True,
            allocation__isnull=True,
        ).count()
        total = BedSpace.objects.filter(room__hostel=hostel).count()
        hostel_data.append({
            'hostel': hostel,
            'available': avail,
            'total': total,
            'full': avail == 0,
        })

    # Total available across all hostels
    total_available = BedSpace.objects.filter(
        is_available=True, allocation__isnull=True
    ).count()
    total_beds = BedSpace.objects.count()
    total_hostels = Hostel.objects.count()

    context = {
        'page_title': 'Hostel & Accommodation',
        'allocation': allocation,
        'allocation_status': allocation.get_status_display() if allocation else None,
        'hostel_data': hostel_data,
        'total_available': total_available,
        'total_beds': total_beds,
        'total_hostels': total_hostels,
        'has_allocation': allocation is not None,
        'academic_year': '2025/2026',
    }
    return render(request, 'hostels/student_dashboard.html', context)


@login_required
def apply_for_bed(request):
    """
    Student submits application for a specific bed space.
    POST: hostel_id → show available beds in that hostel
          bed_space_id → create allocation
    """
    user = request.user

    # Check if already allocated
    if HostelAllocation.objects.filter(
        student=user, status__in=['pending_payment', 'paid']
    ).exists():
        messages.warning(request, 'You already have an active hostel allocation.')
        return redirect('hostels:student_dashboard')

    if request.method == 'POST':
        bed_space_id = request.POST.get('bed_space_id')
        if not bed_space_id:
            messages.error(request, 'Please select a bed space to apply.')
            return redirect('hostels:apply_allocation')

        bed_space = get_object_or_404(BedSpace, pk=bed_space_id)

        # Check availability
        if not bed_space.is_available or hasattr(bed_space, 'allocation'):
            try:
                _ = bed_space.allocation  # Will raise if no allocation
                messages.error(request, 'That bed is no longer available. Please choose another.')
                return redirect('hostels:apply_allocation')
            except Exception:
                pass  # No allocation exists yet — safe to continue

        # Create allocation
        HostelAllocation.objects.create(
            student=user,
            bed_space=bed_space,
            academic_year='2025/2026',
            status='pending_payment',
        )
        # Mark bed as unavailable
        bed_space.is_available = False
        bed_space.save(update_fields=['is_available'])

        messages.success(
            request,
            f'Bed {bed_space.bed_identifier} in {bed_space.room.hostel.name} — '
            f'Room {bed_space.room.room_number} has been reserved. '
            'Please complete payment to confirm your allocation.'
        )
        return redirect('hostels:student_dashboard')

    # GET: show all hostels and their available beds
    hostel_id = request.GET.get('hostel')
    selected_hostel = None
    available_beds = []

    if hostel_id:
        selected_hostel = get_object_or_404(Hostel, pk=hostel_id)
        # Get beds that are free
        occupied_bed_ids = HostelAllocation.objects.filter(
            status__in=['pending_payment', 'paid']
        ).values_list('bed_space_id', flat=True)

        available_beds = BedSpace.objects.filter(
            room__hostel=selected_hostel,
            is_available=True,
        ).exclude(id__in=occupied_bed_ids).select_related('room')

    hostels = Hostel.objects.all()

    context = {
        'page_title': 'Apply for Hostel Accommodation',
        'hostels': hostels,
        'selected_hostel': selected_hostel,
        'available_beds': available_beds,
    }
    return render(request, 'hostels/apply.html', context)


@login_required
def cancel_my_allocation(request, allocation_id):
    """Student cancels their own pending allocation."""
    allocation = get_object_or_404(
        HostelAllocation, pk=allocation_id, student=request.user
    )

    if allocation.status not in ['pending_payment']:
        messages.error(request, 'Only pending allocations can be cancelled.')
        return redirect('hostels:student_dashboard')

    bed = allocation.bed_space
    allocation.status = 'cancelled'
    allocation.save(update_fields=['status'])

    bed.is_available = True
    bed.save(update_fields=['is_available'])

    messages.success(request, 'Your hostel allocation has been cancelled and the bed released.')
    return redirect('hostels:student_dashboard')


# ─────────────────────────────────────────────────────────────
#  ADMIN / STAFF VIEWS
# ─────────────────────────────────────────────────────────────

@login_required
def admin_hostel_dashboard(request):
    """Hostel admin: overview of all allocations, hostels and bed occupancy."""
    if request.user.role not in [
        'admin_officer', 'dean_students_affairs', 'deputy_dean_students_affairs',
        'ict_director', 'director', 'provost', 'registrar',
    ]:
        messages.error(request, 'You do not have permission to access hostel administration.')
        return redirect('portal:dashboard')

    hostels = Hostel.objects.prefetch_related('rooms__bed_spaces').all()
    allocations = HostelAllocation.objects.select_related(
        'student', 'bed_space__room__hostel'
    ).order_by('-allocated_at')

    total_beds = BedSpace.objects.count()
    occupied = HostelAllocation.objects.filter(
        status__in=['pending_payment', 'paid']
    ).count()
    available = total_beds - occupied
    paid = HostelAllocation.objects.filter(status='paid').count()
    pending = HostelAllocation.objects.filter(status='pending_payment').count()

    context = {
        'page_title': 'Hostel Administration',
        'hostels': hostels,
        'allocations': allocations,
        'total_beds': total_beds,
        'occupied': occupied,
        'available': available,
        'paid': paid,
        'pending': pending,
    }
    return render(request, 'hostels/admin_dashboard.html', context)


@login_required
def admin_confirm_payment(request, allocation_id):
    """
    Admin confirms payment for a pending hostel allocation.

    Behaviour:
    - If the allocation already has a Paystack payment_reference, the gateway
      is queried first and the allocation is only marked paid when Paystack
      confirms the charge succeeded.
    - If a manual payment_ref is supplied (cash / bank transfer), the admin
      can override and mark as paid directly — this is recorded with
      payment_gateway='manual' so it is auditable.
    - Admins with appropriate roles can always force-confirm regardless of
      gateway status (e.g. for offline payments).
    """
    if request.method != 'POST':
        return redirect('hostels:admin_dashboard')

    allocation = get_object_or_404(HostelAllocation, pk=allocation_id)
    manual_ref = request.POST.get('payment_ref', '').strip()
    force_confirm = request.POST.get('force_confirm', '') == '1'

    # ── Case 1: allocation has a Paystack reference — verify with gateway ──
    if allocation.payment_reference and not force_confirm:
        try:
            gateway = PaystackGateway()
            result = gateway.verify_payment(allocation.payment_reference)
        except PaystackError as exc:
            logger.error(
                "Admin Paystack verification failed for allocation %s ref %s: %s",
                allocation.pk, allocation.payment_reference, exc,
            )
            messages.error(
                request,
                f'Gateway verification failed: {exc}. '
                'Use "Force Confirm" to override for offline payments.',
            )
            return redirect('hostels:admin_dashboard')

        if result['paid']:
            allocation.status = 'paid'
            allocation.payment_date = timezone.now()
            allocation.gateway_response = result['raw']
            allocation.save(update_fields=['status', 'payment_date', 'gateway_response'])
            logger.info(
                "HostelAllocation %s confirmed by admin via Paystack (ref=%s, admin=%s)",
                allocation.pk, allocation.payment_reference, request.user.username,
            )
            messages.success(
                request,
                f'Payment confirmed for {allocation.student.get_full_name()} — '
                f'{allocation.bed_space.room.hostel.name} '
                f'Room {allocation.bed_space.room.room_number} '
                f'(Paystack verified ✓)',
            )
        else:
            messages.warning(
                request,
                f'Paystack reports payment status as "{result["status"]}" for '
                f'{allocation.student.get_full_name()}. '
                'Use "Force Confirm" to override for offline payments.',
            )
        return redirect('hostels:admin_dashboard')

    # ── Case 2: manual / offline payment or force-confirm ─────────────────
    if manual_ref:
        allocation.payment_reference = manual_ref
    allocation.status = 'paid'
    allocation.payment_date = timezone.now()
    allocation.payment_gateway = 'manual'
    allocation.save(update_fields=[
        'status', 'payment_date', 'payment_reference', 'payment_gateway'
    ])
    logger.info(
        "HostelAllocation %s manually confirmed by admin %s (ref=%s)",
        allocation.pk, request.user.username, allocation.payment_reference,
    )
    messages.success(
        request,
        f'Payment manually confirmed for {allocation.student.get_full_name()} — '
        f'{allocation.bed_space.room.hostel.name} '
        f'Room {allocation.bed_space.room.room_number}',
    )
    return redirect('hostels:admin_dashboard')


@login_required
def admin_cancel_allocation(request, allocation_id):
    """Admin cancels any allocation and frees the bed."""
    if request.method == 'POST':
        allocation = get_object_or_404(HostelAllocation, pk=allocation_id)
        bed = allocation.bed_space
        allocation.status = 'cancelled'
        allocation.save(update_fields=['status'])
        bed.is_available = True
        bed.save(update_fields=['is_available'])
        messages.success(
            request,
            f'Allocation for {allocation.student.get_full_name()} cancelled.'
        )
    return redirect('hostels:admin_dashboard')

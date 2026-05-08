"""
Portal views — all HTML page views served via Django templates.
Yar'yaya College of Health Science and Technology, Sanga.
"""
import re
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.db.models import Count, Q, Avg, Sum
from django.http import JsonResponse, HttpResponse
from django.conf import settings

from accounts.models import User, StudentProfile, StaffProfile
from students.models import CourseRegistration
from courses.models import Course, CourseOffering
from results.models import Result, SemesterResult
from fees.models import FeePayment, FeeStructure
from notifications.models import Notification
from ict_director.models import AuditLog, GeneralPermission, OTPRecord
from staff.models import CourseAllocation, Scoresheet
from admissions.models import ApplicationRecord

from .decorators import role_required, login_required_portal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from .utils import _get_client_ip, _audit
from hostels.models import Hostel, Room, BedSpace, HostelAllocation

CURRENT_YEAR = getattr(settings, 'CURRENT_ACADEMIC_YEAR', '2025/2026')


def _normalize_matric(value):
    """Normalize a matric number for lookup: preserves slashes, strips whitespace."""
    return value.strip()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def login_view(request):
    """
    Premium login page with brute-force protection.
    Accepts matric number (for students) or username.
    """
    if request.user.is_authenticated:
        return redirect('portal:dashboard')

    if request.method == 'POST':
        username_input = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        ip = _get_client_ip(request)

        # Try exact username first, then search by matriculation number in StudentProfile
        user_obj = None
        try:
            user_obj = User.objects.get(username=username_input)
        except User.DoesNotExist:
            # Try looking up by matric number stored in StudentProfile
            try:
                sp = StudentProfile.objects.get(matriculation_number__iexact=username_input)
                user_obj = sp.user
            except StudentProfile.DoesNotExist:
                user_obj = None

        # Lockout check
        if user_obj and user_obj.is_locked:
            _audit(request, 'FAILED_LOGIN', 'AUTH', username_input, status='failed',
                   details=f'Account locked. IP: {ip}')
            messages.error(
                request,
                f'Account locked until {user_obj.account_locked_until.strftime("%H:%M")}. '
                'Contact the ICT Director for assistance.'
            )
            return render(request, 'auth/login.html')

        # Authenticate using the actual username from DB
        actual_username = user_obj.username if user_obj else username_input
        user = authenticate(request, username=actual_username, password=password)

        if user:
            user.reset_failed_login()
            user.last_login_ip = ip
            user.save(update_fields=['last_login_ip'])
            login(request, user)
            request.session['last_activity'] = timezone.now().timestamp()
            _audit(request, 'LOGIN', 'AUTH', user.pk, status='success',
                   details=f'Login from {ip}')

            messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
            return redirect('portal:dashboard')
        else:
            if user_obj:
                user_obj.increment_failed_login()
                remaining = max(0, 5 - user_obj.failed_login_attempts)
                _audit(request, 'FAILED_LOGIN', 'AUTH', username_input, status='failed',
                       details=f'Wrong password. IP: {ip}')
                if remaining > 0:
                    messages.error(request, f'Invalid credentials. {remaining} attempt(s) remaining.')
                else:
                    messages.error(request, 'Account locked for 15 minutes due to too many failed attempts.')
            else:
                messages.error(request, 'Invalid matric number/username or password.')

    return render(request, 'auth/login.html')


def logout_view(request):
    """Log user out and redirect to login."""
    if request.user.is_authenticated:
        _audit(request, 'LOGOUT', 'AUTH', request.user.pk)
    logout(request)
    messages.info(request, 'You have been logged out successfully.')
    return redirect('portal:login')


def get_password_view(request):
    """
    Public portal for new students/applicants to retrieve their login credentials.
    Student enters their matric number. System returns their default password info.
    """
    result = None
    error = None

    if request.method == 'POST':
        matric_input = request.POST.get('matric_number', '').strip()
        if not matric_input:
            error = 'Please enter your matric number.'
        else:
            try:
                profile = StudentProfile.objects.select_related('user').get(
                    matriculation_number__iexact=matric_input
                )
                result = {
                    'full_name': profile.user.get_full_name(),
                    'matric_number': profile.matriculation_number,
                    'department': profile.department,
                    'level': profile.level,
                    'username': profile.matriculation_number,
                    'default_password': '12345678',
                    'must_change': profile.user.must_change_password,
                }
            except StudentProfile.DoesNotExist:
                error = 'No student record found for that matric number. Contact the Registrar.'

    return render(request, 'auth/get_password.html', {
        'result': result,
        'error': error,
    })



# ---------------------------------------------------------------------------
# Dashboard — personalised per role
# ---------------------------------------------------------------------------
@login_required_portal
def dashboard_view(request):
    """Render role-specific dashboard."""
    user = request.user
    role = user.role
    context = {'page_title': 'Dashboard'}

    if role == 'student':
        try:
            profile = user.student_profile
        except StudentProfile.DoesNotExist:
            profile = None

        # Clearance status for dashboard
        clearance = None
        try:
            from clearance.models import StudentClearance
            clearance = user.clearance
        except Exception:
            pass

        fees_cleared, total_paid, total_due = _check_student_clearance(user)
        balance = max(0, total_due - total_paid)
        
        registrations = CourseRegistration.objects.filter(
            student=user, academic_year='2024/2025'
        )
        semester_results = SemesterResult.objects.filter(student=user).order_by('-academic_year', '-semester')
        unpaid = FeePayment.objects.filter(student=user, status='pending').count()
        recent_results = Result.objects.filter(student=user, status='published').order_by('-created_at')[:5]
        notices = Notification.objects.filter(
            Q(recipient=user) | Q(recipient_role='student') | Q(recipient_role='all'),
            is_read=False
        ).order_by('-created_at')[:5]

        # Determine if student has matric (needed for fees/course reg gating)
        has_matric = profile is not None and bool(profile.matriculation_number)

        context.update({
            'profile': profile,
            'clearance': clearance,
            'has_matric': has_matric,
            'registrations': registrations,
            'registration_count': registrations.count(),
            'total_units': sum(r.credit_units for r in registrations),
            'semester_results': semester_results[:4],
            'latest_gpa': semester_results.first().gpa if semester_results.exists() else None,
            'pending_fee_count': unpaid,
            'recent_results': recent_results,
            'notices': notices,
            'fees_cleared': fees_cleared,
            'fee_details': {'total_paid': total_paid, 'total_due': total_due, 'balance': balance},
            'grading_scale': profile.get_grading_scale() if profile else 4.0,
            'current_academic_year': CURRENT_YEAR,
            'profile_completed': profile.profile_completed if profile else False,
        })
        return render(request, 'dashboard/student.html', context)

    elif role in ['lecturer', 'practical_master']:
        allocations = CourseAllocation.objects.filter(staff=user, is_active=True, academic_year=CURRENT_YEAR)
        pending_scoresheets = Scoresheet.objects.filter(
            uploaded_by=user, status='submitted'
        ).count()
        context.update({
            'course_count': allocations.count(),
            'allocations': allocations[:5],
            'pending_scoresheets': pending_scoresheets,
        })
        return render(request, 'dashboard/lecturer.html', context)

    elif role in ['hod', 'hod_coordinator']:
        dept_courses = Course.objects.filter(department=_get_dept(user))
        dept_staff = StaffProfile.objects.filter(department=_get_dept(user))
        pending_scores = Scoresheet.objects.filter(status='submitted').count()
        context.update({
            'dept_course_count': dept_courses.count(),
            'dept_staff_count': dept_staff.count(),
            'pending_scores': pending_scores,
        })
        return render(request, 'dashboard/hod.html', context)

    elif role in ['dean_students_affairs', 'deputy_dean_students_affairs']:
        context.update({
            'total_students': StudentProfile.objects.count(),
            'total_staff': StaffProfile.objects.count(),
            'pending_scores': Scoresheet.objects.filter(status='submitted').count(),
        })
        return render(request, 'dashboard/dean.html', context)

    elif role == 'bursary':
        total_paid = FeePayment.objects.filter(status='completed').count()
        pending_payments = FeePayment.objects.filter(status='pending').count()
        context.update({
            'total_paid_count': total_paid,
            'pending_payments': pending_payments,
            'recent_payments': FeePayment.objects.select_related('student').order_by('-payment_date')[:10],
        })
        return render(request, 'dashboard/bursary.html', context)

    elif role in ['registrar', 'deputy_registrar', 'academic_secretary', 'admin_officer', 'liaison_officer']:
        context.update({
            'total_students': StudentProfile.objects.count(),
            'total_courses': Course.objects.filter(is_active=True).count(),
            'pending_permissions': GeneralPermission.objects.filter(is_active=True).count(),
        })
        return render(request, 'dashboard/registrar.html', context)

    elif role == 'exams_officer':
        context.update({
            'pending_results': Result.objects.filter(status='approved').count(),
            'published_results': Result.objects.filter(status='published').count(),
        })
        return render(request, 'dashboard/exams_officer.html', context)

    elif role == 'ict_director':
        context.update({
            'total_users': User.objects.count(),
            'locked_accounts': User.objects.filter(
                account_locked_until__gt=timezone.now()
            ).count(),
            'recent_audit_logs': AuditLog.objects.order_by('-created_at')[:10],
            'pending_otps': OTPRecord.objects.filter(status='generated').count(),
            'pending_permissions': GeneralPermission.objects.filter(is_active=True).count(),
        })
        return render(request, 'dashboard/ict_director.html', context)

    elif role in ['provost', 'director']:
        context.update({
            'total_students': StudentProfile.objects.count(),
            'total_staff': StaffProfile.objects.count(),
            'total_courses': Course.objects.filter(is_active=True).count(),
            'total_payments': FeePayment.objects.filter(status='completed').count(),
            'total_paid_amount': FeePayment.objects.filter(status='completed').aggregate(Sum('amount'))['amount__sum'] or 0,
            'total_pending_amount': FeePayment.objects.filter(status='pending').aggregate(Sum('amount'))['amount__sum'] or 0,
            'all_users': User.objects.all().select_related('student_profile').order_by('-date_joined')[:100], 
            'recent_signups': User.objects.filter(date_joined__gte=timezone.now() - timedelta(days=7)).order_by('-date_joined'),
            'student_list': StudentProfile.objects.select_related('user').order_by('user__last_name')[:50],
            'staff_list': StaffProfile.objects.select_related('user').order_by('user__last_name')[:50],
            'pending_admissions': ApplicationRecord.objects.filter(status='submitted').count(),
            'total_admissions': ApplicationRecord.objects.filter(status='admitted').count(),
            'total_applications': ApplicationRecord.objects.count(),
        })
        return render(request, 'dashboard/executive.html', context)

    # Fallback for roles without a dashboard or staff with no specific role handle
    if user.is_staff:
        return render(request, 'dashboard/registrar.html', context)
    return render(request, 'dashboard/student.html', context)


def _get_dept(user):
    try:
        return user.staff_profile.department
    except Exception:
        return ''


def _get_faculty(user):
    try:
        return user.staff_profile.faculty
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@login_required_portal
def profile_view(request):
    """View/edit current user's profile."""
    user = request.user
    profile = None
    try:
        if user.role == 'student':
            profile = user.student_profile
        else:
            profile = user.staff_profile
    except Exception:
        pass

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        theme = request.POST.get('theme_preference', 'light')

        user.first_name = first_name
        user.last_name = last_name
        user.phone_number = phone_number
        user.theme_preference = theme
        user.save(update_fields=['first_name', 'last_name', 'phone_number', 'theme_preference'])
        _audit(request, 'USER_UPDATE', 'USER', user.pk, details='Profile updated')
        messages.success(request, 'Profile updated successfully.')
        return redirect('portal:profile')

    return render(request, 'profile/profile.html', {'profile': profile})


# ---------------------------------------------------------------------------
# Student Biodata & Onboarding
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('student')
def complete_biodata(request):
    """
    First-login biodata completion form for newly admitted students.
    Students must complete this to activate their account.
    """
    from accounts.forms import StudentBioDataForm
    
    user = request.user
    
    try:
        student_profile = user.student_profile
    except StudentProfile.DoesNotExist:
        messages.error(request, 'Student profile not found. Please contact the registrar.')
        return redirect('portal:dashboard')
    
    # If profile already completed, redirect to dashboard
    if student_profile.profile_completed:
        messages.info(request, 'Your biodata has already been completed.')
        return redirect('portal:dashboard')
    
    if request.method == 'POST':
        form = StudentBioDataForm(request.POST, instance=student_profile, user=user)
        if form.is_valid():
            form.save()
            _audit(request, 'USER_UPDATE', 'BIODATA_COMPLETED', user.pk, details='Student completed biodata registration')
            messages.success(request, 'Biodata saved successfully! Your account is now fully activated.')
            return redirect('portal:dashboard')
    else:
        form = StudentBioDataForm(instance=student_profile, user=user)
    
    context = {
        'form': form,
        'student_profile': student_profile,
        'is_first_login': not student_profile.profile_completed,
    }
    
    return render(request, 'profile/complete_biodata.html', context)


@login_required_portal
def change_password(request):
    """
    Allow any user to change their password.
    Required on first login when must_change_password=True.
    """
    user = request.user
    is_forced = user.must_change_password  # Inform template if this is forced

    if request.method == 'POST':
        old_password = request.POST.get('old_password', '')
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')

        if not user.check_password(old_password):
            messages.error(request, 'Current/default password is incorrect.')
            return render(request, 'profile/change_password.html', {'is_forced': is_forced})

        if len(new_password) < 8:
            messages.error(request, 'New password must be at least 8 characters.')
            return render(request, 'profile/change_password.html', {'is_forced': is_forced})

        if new_password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'profile/change_password.html', {'is_forced': is_forced})

        if new_password == '12345678':
            messages.error(request, 'You cannot use the default password. Choose a strong personal password.')
            return render(request, 'profile/change_password.html', {'is_forced': is_forced})

        # Set new password and clear the forced flag
        user.set_password(new_password)
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])
        update_session_auth_hash(request, user)  # Keep user logged in
        _audit(request, 'USER_UPDATE', 'PASSWORD_CHANGE', user.pk,
               details='Password changed successfully')
        messages.success(request, '✅ Password changed successfully! Welcome to YCHST Portal.')
        return redirect('portal:dashboard')

    return render(request, 'profile/change_password.html', {'is_forced': is_forced})


@login_required_portal
@role_required('student')
def update_biodata(request):
    """
    Existing enrolled students update their biodata (personal/contact info).
    Accessible from the student dashboard at any time.
    """
    user = request.user
    try:
        profile = user.student_profile
    except StudentProfile.DoesNotExist:
        messages.error(request, 'Student profile not found. Contact the Registrar.')
        return redirect('portal:dashboard')

    if request.method == 'POST':
        # Contact info
        phone_number = request.POST.get('phone_number', '').strip()
        gender = request.POST.get('gender', '').strip()
        dob = request.POST.get('date_of_birth', '').strip()
        address = request.POST.get('address', '').strip()

        # Personal details
        state_of_origin = request.POST.get('state_of_origin', '').strip()
        local_government = request.POST.get('local_government', '').strip()
        religion = request.POST.get('religion', '').strip()
        nationality = request.POST.get('nationality', 'Nigerian').strip()

        # Next of kin
        nok_name = request.POST.get('next_of_kin_name', '').strip()
        nok_phone = request.POST.get('next_of_kin_phone', '').strip()
        nok_rel = request.POST.get('next_of_kin_relationship', '').strip()
        nok_addr = request.POST.get('next_of_kin_address', '').strip()

        # Sponsor
        sponsor_name = request.POST.get('sponsor_name', '').strip()
        sponsor_phone = request.POST.get('sponsor_phone', '').strip()
        sponsor_occ = request.POST.get('sponsor_occupation', '').strip()

        # Update User
        user.phone_number = phone_number
        user.gender = gender
        if dob:
            from datetime import date
            try:
                from datetime import datetime
                user.date_of_birth = datetime.strptime(dob, '%Y-%m-%d').date()
            except ValueError:
                pass
        user.address = address
        user.save(update_fields=['phone_number', 'gender', 'date_of_birth', 'address'])

        # Update StudentProfile
        profile.state_of_origin = state_of_origin
        profile.local_government = local_government
        profile.nationality = nationality
        profile.religion = religion
        profile.next_of_kin_name = nok_name
        profile.next_of_kin_phone = nok_phone
        profile.next_of_kin_relationship = nok_rel
        profile.next_of_kin_address = nok_addr
        profile.sponsor_name = sponsor_name
        profile.sponsor_phone = sponsor_phone
        profile.sponsor_occupation = sponsor_occ
        profile.profile_completed = True
        profile.save()

        # Handle profile picture upload
        if 'profile_picture' in request.FILES:
            user.profile_picture = request.FILES['profile_picture']
            user.save(update_fields=['profile_picture'])

        _audit(request, 'USER_UPDATE', 'BIODATA', user.pk, details='Student updated biodata')
        messages.success(request, '✅ Biodata updated successfully!')
        return redirect('portal:profile')

    return render(request, 'profile/update_biodata.html', {
        'profile': profile,
        'page_title': 'Update Biodata',
    })


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('student')
def course_registration_view(request):
    user = request.user
    
    # 0. Enforce Matric Number Dependency — must have matric before course reg
    try:
        student_profile = user.student_profile
        if not student_profile.matriculation_number:
            raise AttributeError
    except (StudentProfile.DoesNotExist, AttributeError):
        messages.error(request, 'You must complete clearance and receive your matric number before registering courses.')
        return redirect('clearance:dashboard')

    # 1. Enforce Fee Clearance Dependency
    fees_cleared, total_paid, total_due = _check_student_clearance(user)

    available_courses = CourseOffering.objects.filter(
        is_active=True,
        course__is_active=True,
    ).select_related('course')

    try:
        student_profile = user.student_profile
        available_courses = available_courses.filter(
            course__level=student_profile.level,
            course__department=student_profile.department,
        )
    except Exception:
        student_profile = None

    registrations = CourseRegistration.objects.filter(
        student=user, academic_year='2024/2025'
    )
    registered_codes = registrations.values_list('course_code', flat=True)

    if request.method == 'POST':
        if not fees_cleared:
            messages.error(request, 'You cannot register courses until your school fees are fully cleared.')
            return redirect('portal:course_registration')
            
        selected_codes = request.POST.getlist('courses')
        academic_year = '2024/2025'
        semester = request.POST.get('semester', 'first')

        # 2. Enforce 24 Max Credit Load Limit
        current_units = CourseRegistration.objects.filter(
            student=user, academic_year=academic_year, semester=semester
        ).aggregate(total=Sum('credit_units'))['total'] or 0
        
        units_to_add = 0
        offerings_to_register = []
        for code in selected_codes:
            if code not in registered_codes:
                offering = available_courses.filter(course__course_code=code).first()
                if offering:
                    offerings_to_register.append(offering)
                    units_to_add += offering.course.credit_units
                    
        if current_units + units_to_add > 24:
            messages.error(request, f'Registration failed: Max credit load is 24. You are trying to register {current_units + units_to_add} units.')
            return redirect('portal:course_registration')

        created_count = 0
        for offering in offerings_to_register:
            CourseRegistration.objects.get_or_create(
                student=user,
                course_code=offering.course.course_code,
                academic_year=academic_year,
                semester=semester,
                defaults={
                    'course_title': offering.course.course_title,
                    'credit_units': offering.course.credit_units,
                    'status': 'pending_exams'
                }
            )
            created_count += 1

        _audit(request, 'COURSE_REGISTER', 'COURSE', user.pk,
               new_value={'courses': selected_codes},
               details=f'Registered {created_count} courses')
        messages.success(request, f'Successfully registered {created_count} course(s).')
        return redirect('portal:course_registration')

    context = {
        'available_courses': available_courses,
        'registered_codes': list(registered_codes),
        'registrations': registrations,
        'total_registered_units': registrations.aggregate(total=Sum('credit_units'))['total'] or 0,
        'profile': student_profile,
        'fees_cleared': fees_cleared,
        'page_title': 'Course Registration',
    }
    return render(request, 'courses/register.html', context)


@login_required_portal
@role_required('exams_officer', 'hod', 'registrar', 'ict_director')
def registration_approval_view(request):
    """
    Unified dashboard for staff to approve student course registrations.
    Workflow: pending_exams -> pending_hod -> pending_registrar -> approved
    """
    user = request.user
    role = user.role
    
    # Define which status this user handles
    status_map = {
        'exams_officer': 'pending_exams',
        'hod': 'pending_hod',
        'registrar': 'pending_registrar',
        'ict_director': 'pending_exams', # Admin can see first stage
    }
    
    # Define what the next status is
    next_status_map = {
        'pending_exams': 'pending_hod',
        'pending_hod': 'pending_registrar',
        'pending_registrar': 'approved',
    }

    target_status = status_map.get(role, 'pending_exams')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        registration_ids = request.POST.getlist('registration_ids')
        rejection_reason = request.POST.get('rejection_reason', '')
        
        if not registration_ids:
            messages.warning(request, "No students selected.")
            return redirect('portal:registration_approvals')
            
        # Get matching registrations
        registrations = CourseRegistration.objects.filter(
            pk__in=registration_ids,
            status=target_status
        )
        
        # Security: HOD and Exams Officer only see their department
        if role in ['hod', 'exams_officer']:
            dept = getattr(user.staff_profile, 'department', None)
            if dept:
                registrations = registrations.filter(student__student_profile__department=dept)
        
        if action == 'approve':
            next_status = next_status_map.get(target_status, 'approved')
            count = registrations.count()
            
            # Update approval fields
            update_data = {'status': next_status, 'updated_at': timezone.now()}
            if target_status == 'pending_exams':
                update_data.update({'exams_approved_by': user, 'exams_approved_at': timezone.now()})
            elif target_status == 'pending_hod':
                update_data.update({'hod_approved_by': user, 'hod_approved_at': timezone.now()})
            elif target_status == 'pending_registrar':
                update_data.update({'registrar_approved_by': user, 'registrar_approved_at': timezone.now()})
            
            registrations.update(**update_data)
            
            # Create notifications for the next stage or final student
            target_registrations = CourseRegistration.objects.filter(pk__in=registration_ids)
            for reg in target_registrations:
                if next_status == 'approved':
                    Notification.objects.create(
                        recipient=reg.student,
                        title='Course Registration Approved',
                        message=f'Your course registration for {reg.academic_year} has been fully approved by the Registrar.',
                        notification_type='success'
                    )
                else:
                    # Notify the role responsible for the NEXT status
                    role_map = {'pending_hod': 'hod', 'pending_registrar': 'registrar'}
                    target_role = role_map.get(next_status)
                    if target_role:
                        Notification.objects.create(
                            recipient_role=target_role,
                            title='New Registration Pending Approval',
                            message=f'Student {reg.student.username} has submitted courses for your approval.',
                            notification_type='info'
                        )

            _audit(request, 'COURSE_APPROVE', 'REGISTRATION', user.pk, 
                   details=f'Approved {count} registrations to {next_status}')
            messages.success(request, f"Successfully approved {count} registration(s).")
            
        elif action == 'reject':
            count = registrations.count()
            
            # Notify students
            target_registrations = CourseRegistration.objects.filter(pk__in=registration_ids)
            for reg in target_registrations:
                Notification.objects.create(
                    recipient=reg.student,
                    title='Course Registration Rejected',
                    message=f'Your registration for {reg.course_code} was rejected. Reason: {rejection_reason}',
                    notification_type='warning'
                )
                
            registrations.update(
                status='rejected',
                rejection_reason=rejection_reason,
                updated_at=timezone.now()
            )
            _audit(request, 'COURSE_REJECT', 'REGISTRATION', user.pk, 
                   details=f'Rejected {count} registrations. Reason: {rejection_reason}')
            messages.error(request, f"Rejected {count} registration(s).")
            
        return redirect('portal:registration_approvals')

    # GET: Filter registrations for the dashboard
    pending_list = CourseRegistration.objects.filter(status=target_status).select_related(
        'student', 'student__student_profile'
    ).order_by('student__student_profile__department', 'student__username')
    
    # Departmental filtering
    if role in ['hod', 'exams_officer']:
        staff_profile = getattr(user, 'staff_profile', None)
        dept = getattr(staff_profile, 'department', None)
        if dept:
            pending_list = pending_list.filter(student__student_profile__department=dept)
            
    # Group by student for the UI
    from itertools import groupby
    student_groups = []
    # Sort for groupby
    pending_list = sorted(pending_list, key=lambda x: x.student.id)
    for student_id, items in groupby(pending_list, key=lambda x: x.student):
        reg_items = list(items)
        student_groups.append({
            'student': reg_items[0].student,
            'profile': getattr(reg_items[0].student, 'student_profile', None),
            'registrations': reg_items,
            'total_units': sum(r.credit_units for r in reg_items),
            'ids': [str(r.pk) for r in reg_items]
        })

    context = {
        'student_groups': student_groups,
        'role_label': dict(User.ROLE_CHOICES).get(role, role),
        'target_status': target_status,
        'page_title': 'Registration Approvals',
    }
    return render(request, 'courses/registration_approvals.html', context)



@login_required_portal
@role_required('lecturer', 'hod', 'dean_students_affairs', 'deputy_dean_students_affairs')
def my_courses_view(request):
    user = request.user
    allocations = CourseAllocation.objects.filter(
        staff=user, is_active=True
    ).order_by('-academic_year')
    return render(request, 'courses/my_courses.html', {
        'allocations': allocations,
        'page_title': 'My Courses',
    })


@login_required_portal
@role_required('exams_officer', 'registrar', 'ict_director', 'hod')
def manage_offerings_view(request):
    """Exams Officer: view, create, update and delete course offerings."""

    academic_year  = request.GET.get('academic_year', '2024/2025')
    prog_filter    = request.GET.get('programme', '')
    level_filter   = request.GET.get('level', '')

    if request.method == 'POST':
        action = request.POST.get('action')
        target_year = request.POST.get('academic_year', academic_year)

        # ── Bulk Create ──
        if action == 'bulk_create':
            course_ids = request.POST.getlist('course_id')
            capacity = request.POST.get('class_capacity', 50)
            try:
                capacity = int(capacity)
            except ValueError:
                capacity = 50

            created_count = 0
            for cid in course_ids:
                try:
                    course = Course.objects.get(pk=cid)
                    _, created = CourseOffering.objects.get_or_create(
                        course=course,
                        academic_year=target_year,
                        defaults={'class_capacity': capacity, 'is_active': True},
                    )
                    if created:
                        created_count += 1
                except Course.DoesNotExist:
                    continue

            if created_count > 0:
                messages.success(request, f'Successfully offered {created_count} courses in {target_year}.')
            else:
                messages.warning(request, 'No new courses were added. They may already exist in this session.')
            return redirect(f'{request.path}?academic_year={target_year}')

        # ── Create (Single) ──
        elif action == 'create':
            course_id = request.POST.get('course_id')
            capacity  = request.POST.get('class_capacity', 50)
            try:
                course = Course.objects.get(pk=course_id)
                offering, created = CourseOffering.objects.get_or_create(
                    course=course,
                    academic_year=target_year,
                    defaults={'class_capacity': int(capacity), 'is_active': True},
                )
                if created:
                    _audit(request, 'OFFERING_CREATE', 'OFFERING', offering.pk,
                           details=f'Offering created: {course.course_code} ({target_year})')
                    messages.success(request, f'Offering for {course.course_code} created.')
                else:
                    messages.warning(request, f'{course.course_code} already offered in {target_year}.')
            except (Course.DoesNotExist, ValueError) as e:
                messages.error(request, f'Error: {e}')
            return redirect(f'{request.path}?academic_year={target_year}')

        # ── Edit / Update ──
        elif action == 'edit':
            offering_id  = request.POST.get('offering_id')
            capacity     = request.POST.get('class_capacity', 50)
            lecturer_id  = request.POST.get('lecturer_id', '')
            venue        = request.POST.get('venue', '')
            day          = request.POST.get('day_of_week', '')
            start_time   = request.POST.get('start_time', '') or None
            end_time     = request.POST.get('end_time', '')   or None
            try:
                offering = CourseOffering.objects.get(pk=offering_id)
                offering.class_capacity = int(capacity)
                offering.venue = venue
                offering.day_of_week = day
                offering.start_time  = start_time
                offering.end_time    = end_time
                if lecturer_id:
                    try:
                        offering.lecturer = User.objects.get(pk=lecturer_id)
                    except User.DoesNotExist:
                        pass
                else:
                    offering.lecturer = None
                offering.save()
                _audit(request, 'OFFERING_UPDATE', 'OFFERING', offering.pk,
                       details=f'Updated: {offering.course.course_code}')
                messages.success(request, f'{offering.course.course_code} updated.')
            except CourseOffering.DoesNotExist:
                messages.error(request, 'Offering not found.')
            return redirect(f'{request.path}?academic_year={academic_year}')

        # ── Delete ──
        elif action == 'delete':
            offering_id = request.POST.get('offering_id')
            try:
                offering = CourseOffering.objects.get(pk=offering_id)
                code = offering.course.course_code
                offering.delete()
                _audit(request, 'OFFERING_DELETE', 'OFFERING', offering_id,
                       details=f'Deleted: {code}')
                messages.success(request, f'Offering for {code} removed.')
            except CourseOffering.DoesNotExist:
                messages.error(request, 'Offering not found.')
            return redirect(f'{request.path}?academic_year={academic_year}')

        # ── Bulk activate session ──
        elif action == 'activate_all':
            programme = request.POST.get('programme', '')
            qs = Course.objects.filter(is_active=True)
            if programme:
                qs = qs.filter(department=programme)
            created_count = 0
            for course in qs:
                _, created = CourseOffering.objects.get_or_create(
                    course=course, academic_year=target_year,
                    defaults={'class_capacity': 50, 'is_active': True},
                )
                if created:
                    created_count += 1
            messages.success(request, f'{created_count} offerings activated for {target_year}.')
            return redirect(f'{request.path}?academic_year={target_year}')

    # ── GET ──
    offerings_qs = CourseOffering.objects.filter(
        academic_year=academic_year
    ).select_related('course', 'lecturer').order_by(
        'course__department', 'course__level', 'course__semester', 'course__course_code'
    )
    if prog_filter:
        offerings_qs = offerings_qs.filter(course__department=prog_filter)
    if level_filter:
        offerings_qs = offerings_qs.filter(course__level=level_filter)

    all_courses   = Course.objects.filter(is_active=True).order_by('course_code')
    all_lecturers = User.objects.filter(role='lecturer', is_active=True).order_by('last_name', 'first_name')
    programmes    = Course.objects.filter(is_active=True).values_list('department', flat=True).distinct().order_by('department')
    levels        = Course.objects.filter(is_active=True).values_list('level', flat=True).distinct().order_by('level')

    # Group offerings by programme → level → semester
    from collections import defaultdict
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for o in offerings_qs:
        grouped[o.course.department][o.course.level][o.course.semester].append(o)

    context = {
        'academic_year':  academic_year,
        'offerings':      offerings_qs,
        'grouped':        dict({k: dict({l: dict(s) for l, s in v.items()}) for k, v in grouped.items()}),
        'all_courses':    all_courses,
        'all_lecturers':  all_lecturers,
        'programmes':     programmes,
        'levels':         levels,
        'prog_filter':    prog_filter,
        'level_filter':   level_filter,
        'page_title':     'Course Offerings Management',
    }
    return render(request, 'courses/manage_offerings.html', context)


@login_required_portal
@role_required('registrar', 'ict_director', 'academic_secretary', 'provost', 'director')
def manage_courses_view(request):
    """Management view for the base Course definitions (Catalog)."""
    search_q = request.GET.get('q', '')
    dept_filter = request.GET.get('department', '')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'create':
            code = request.POST.get('course_code', '').upper().strip()
            title = request.POST.get('course_title', '').strip()
            units = request.POST.get('credit_units', 2)
            level = request.POST.get('level', '100')
            semester = request.POST.get('semester', 'first')
            dept = request.POST.get('department', '').strip()
            
            if not code or not title:
                messages.error(request, "Course code and title are required.")
            else:
                course, created = Course.objects.get_or_create(
                    course_code=code,
                    defaults={
                        'course_title': title,
                        'credit_units': units,
                        'level': level,
                        'semester': semester,
                        'department': dept,
                        'faculty': dept,  # Defaulting faculty to department as per requirement
                        'is_active': True
                    }
                )
                if created:
                    _audit(request, 'COURSE_CREATE', 'COURSE', course.pk, details=f'Created {code}')
                    messages.success(request, f"Course {code} added to catalog.")
                else:
                    messages.warning(request, f"Course {code} already exists.")
                    
        elif action == 'edit':
            cid = request.POST.get('course_id')
            course = get_object_or_404(Course, pk=cid)
            course.course_title = request.POST.get('course_title', course.course_title)
            course.credit_units = request.POST.get('credit_units', course.credit_units)
            course.level = request.POST.get('level', course.level)
            course.semester = request.POST.get('semester', course.semester)
            course.department = request.POST.get('department', course.department)
            course.faculty = course.department
            course.save()
            _audit(request, 'COURSE_UPDATE', 'COURSE', course.pk, details=f'Updated {course.course_code}')
            messages.success(request, f"Course {course.course_code} updated.")
            
        elif action == 'delete':
            cid = request.POST.get('course_id')
            course = get_object_or_404(Course, pk=cid)
            code = course.course_code
            # Check if there are offerings before physical delete, or just deactivate
            if CourseOffering.objects.filter(course=course).exists():
                course.is_active = False
                course.save()
                messages.info(request, f"Course {code} deactivated (has active offerings).")
            else:
                course.delete()
                messages.success(request, f"Course {code} removed from catalog.")
            _audit(request, 'COURSE_DELETE', 'COURSE', cid, details=f'Deleted/Deactivated {code}')

        return redirect('portal:manage_courses')

    # Querying
    courses = Course.objects.all().order_by('department', 'level', 'course_code')
    if search_q:
        courses = courses.filter(Q(course_code__icontains=search_q) | Q(course_title__icontains=search_q))
    if dept_filter:
        courses = courses.filter(department=dept_filter)
        
    departments = Course.objects.values_list('department', flat=True).distinct().order_by('department')
    
    context = {
        'courses': courses,
        'departments': departments,
        'page_title': 'Academic Course Catalog',
        'search_q': search_q,
        'dept_filter': dept_filter,
    }
    return render(request, 'courses/manage_courses.html', context)



# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def _check_student_clearance(user):
    """Utility to check if a student has cleared their fees for the session."""
    try:
        from fees.models import FeeStructure, FeePayment, FeeItem
        total_paid = sum(p.amount for p in FeePayment.objects.filter(student=user, status='completed'))

        # Use dynamic FeeItems if available, else fall back to FeeStructure
        fee_items = FeeItem.objects.filter(is_active=True, is_mandatory=True)
        if fee_items.exists():
            total_due = fee_items.aggregate(models.Sum('amount'))['amount__sum'] or 0
        else:
            fee_structure = FeeStructure.objects.filter(is_active=True).first()
            total_due = fee_structure.total_fee if fee_structure else 0

        cleared = (total_paid >= total_due) and total_due > 0
        return cleared, total_paid, total_due
    except Exception:
        return False, 0, 0

@login_required_portal
@role_required('student')
def student_results_view(request):
    user = request.user
    fees_cleared, total_paid, total_due = _check_student_clearance(user)

    if not fees_cleared:
        messages.error(request, f'Your results are withheld due to an outstanding fee balance. (Paid: ₦{total_paid:,.2f} / Due: ₦{total_due:,.2f})')
        return render(request, 'results/student_results.html', {
            'fees_cleared': fees_cleared,
            'page_title': 'My Results',
        })

    # Transcript Search Logic
    academic_year = request.GET.get('session')
    semester = request.GET.get('semester')
    
    semester_results = []
    results = []
    search_active = False
    
    if academic_year and semester:
        search_active = True
        semester_results = SemesterResult.objects.filter(
            student=user, academic_year=academic_year, semester=semester
        ).order_by('-academic_year', '-semester')
        
        results = Result.objects.filter(
            student=user, status='published',
            course_offering__academic_year=academic_year,
            course_offering__course__semester=semester
        ).select_related('course_offering__course').order_by('course_offering__course__course_code')

    # For dropdowns
    available_sessions = Result.objects.filter(student=user, status='published').values_list('course_offering__academic_year', flat=True).distinct()

    context = {
        'fees_cleared': fees_cleared,
        'search_active': search_active,
        'semester_results': semester_results,
        'results': results,
        'available_sessions': available_sessions,
        'selected_session': academic_year,
        'selected_semester': semester,
        'student': getattr(user, 'student_profile', None),
        'page_title': 'My Results',
    }
    return render(request, 'results/student_results.html', context)


@login_required_portal
@role_required('lecturer', 'hod', 'dean_students_affairs', 'deputy_dean_students_affairs', 'exams_officer', 'registrar', 'ict_director')
def score_entry_view(request):
    user = request.user
    allocations = CourseAllocation.objects.filter(staff=user, is_active=True)

    selected_allocation = None
    students_results = []
    if request.GET.get('allocation'):
        try:
            selected_allocation = allocations.get(pk=request.GET['allocation'])
            offerings = CourseOffering.objects.filter(
                course__course_code=selected_allocation.course_code
            )
            if offerings.exists():
                offering = offerings.first()
                students_results = Result.objects.filter(
                    course_offering=offering
                ).select_related('student')
        except CourseAllocation.DoesNotExist:
            pass

    if request.method == 'POST':
        allocation_id = request.POST.get('allocation_id')
        try:
            allocation = allocations.get(pk=allocation_id)
            offering = CourseOffering.objects.filter(
                course__course_code=allocation.course_code
            ).first()
            if offering:
                student_ids = request.POST.getlist('student_ids')
                for sid in student_ids:
                    ca = request.POST.get(f'ca_{sid}')
                    exam = request.POST.get(f'exam_{sid}')
                    if ca and exam:
                        try:
                            student = User.objects.get(pk=sid)
                            Result.objects.update_or_create(
                                student=student,
                                course_offering=offering,
                                defaults={
                                    'continuous_assessment': float(ca),
                                    'examination': float(exam),
                                    'total_score': float(ca) + float(exam),
                                    'status': 'pending',
                                }
                            )
                        except Exception:
                            pass
                # Audit and Response
                _audit(request, 'SCORE_ENTRY', 'RESULT', allocation_id,
                       details=f'Scores entered/updated for course {allocation.course_code}')
                
                # Check if this is a final submission for approval
                if request.POST.get('submit_moderation'):
                    # Create or update scoresheet entry
                    acad_year = '2024/2025' # Should ideally come from settings/context
                    sheet, created = Scoresheet.objects.get_or_create(
                        course_allocation=allocation,
                        uploaded_by=user,
                        academic_year=acad_year,
                        semester=allocation.semester,
                        defaults={'status': 'pending_hod', 'file_name': f'DigitalEntry_{allocation.course_code}'}
                    )
                    if not created:
                        sheet.status = 'pending_hod'
                        sheet.save(update_fields=['status'])
                    
                    # Notify HOD
                    Notification.objects.create(
                        recipient_role='hod',
                        title='New Scoresheet Submitted',
                        message=f'Lecturer {user.username} has submitted scores for {allocation.course_code} for your review.',
                        notification_type='info'
                    )
                    
                    # Set all individual results to pending if they aren't already
                    Result.objects.filter(course_offering__course__course_code=allocation.course_code).update(status='pending')
                    
                    messages.success(request, 'Scores submitted successfully for HOD approval.')
                else:
                    messages.success(request, 'Scores saved successfully as draft.')
                    
        except CourseAllocation.DoesNotExist:
            messages.error(request, 'Invalid course allocation.')
        return redirect(f"{request.path}?allocation={allocation_id}")

    return render(request, 'results/score_entry.html', {
        'allocations': allocations,
        'selected_allocation': selected_allocation,
        'students_results': students_results,
        'page_title': 'Score Entry',
    })


@login_required_portal
@role_required('hod', 'dean_students_affairs', 'deputy_dean_students_affairs', 'exams_officer', 'registrar', 'ict_director')
def scoresheet_view(request):
    """
    Unified Moderation Dashboard for Scoresheets.
    Logic: HOD (Approve) -> Exams (Moderate) -> Dean (Review) -> Registrar (Publish)
    """
    user = request.user
    role = user.role
    
    # Define status mapping based on role
    # Which status is this user looking for?
    role_map = {
        'hod': ('pending_hod', 'HOD Approval', 'pending_exams'),
        'exams_officer': ('pending_exams', 'Exams Moderation', 'pending_dean'),
        'dean': ('pending_dean', 'Dean Review', 'pending_registrar'),
        'registrar': ('pending_registrar', 'Registrar Publication', 'published'),
        'ict_director': ('pending_hod', 'System View (HOD Stage)', 'pending_exams'),
    }
    
    target_status, role_label, next_status = role_map.get(role, ('active', 'Staff', 'active'))
    
    # Filter scoresheets based on role and status
    scoresheet_qs = Scoresheet.objects.select_related('course_allocation', 'uploaded_by').filter(status=target_status)
    
    if role == 'hod':
        # Filter by department
        try:
            dept = user.staff_profile.department
            scoresheet_qs = scoresheet_qs.filter(course_allocation__staff__staff_profile__department=dept)
        except Exception:
            pass

    if request.method == 'POST':
        action = request.POST.get('action')
        sheet_ids = request.POST.getlist('scoresheet_ids')
        
        if not sheet_ids:
            messages.error(request, 'No scoresheets selected.')
            return redirect('portal:scoresheet')
            
        target_sheets = Scoresheet.objects.filter(pk__in=sheet_ids, status=target_status)
        
        if action == 'approve':
            for sheet in target_sheets:
                # Update tracking fields based on role
                if role == 'hod':
                    sheet.hod_approved_by = user
                    sheet.hod_approved_at = timezone.now()
                elif role == 'exams_officer':
                    sheet.exams_validated_by = user
                    sheet.exams_validated_at = timezone.now()
                elif role == 'dean':
                    sheet.dean_reviewed_by = user
                    sheet.dean_reviewed_at = timezone.now()
                elif role == 'registrar':
                    sheet.registrar_published_by = user
                    sheet.registrar_published_at = timezone.now()
                    
                    # FINAL PUBLICATION logic:
                    # Update all linked individual Result objects to 'published'
                    Result.objects.filter(
                        course_offering__course__course_code=sheet.course_allocation.course_code
                    ).update(status='published')
                    
                    # Notify students in this course
                    Notification.objects.create(
                        recipient_role='student', # Simplified, ideally filtered by course
                        title='New Results Published',
                        message=f'Results for {sheet.course_allocation.course_code} have been published. Check your results dashboard.',
                        notification_type='success'
                    )
                
                sheet.status = next_status
                sheet.save()
                
                # Notify the next stage role
                next_role_map = {'pending_exams': 'exams_officer', 'pending_dean': 'dean', 'pending_registrar': 'registrar'}
                target_role = next_role_map.get(next_status)
                if target_role:
                    Notification.objects.create(
                        recipient_role=target_role,
                        title='Scoresheet Awaiting Moderation',
                        message=f'Scoresheet for {sheet.course_allocation.course_code} has moved to your queue.',
                        notification_type='info'
                    )
            
            messages.success(request, f'Successfully approved/moderated {target_sheets.count()} scoresheet(s).')
            
        elif action == 'reject':
            reason = request.POST.get('rejection_reason', 'Rejected by moderator.')
            for sheet in target_sheets:
                Notification.objects.create(
                    recipient=sheet.uploaded_by,
                    title='Scoresheet Returned',
                    message=f'Your scoresheet for {sheet.course_allocation.course_code} was returned for correction. Reason: {reason}',
                    notification_type='warning'
                )
            target_sheets.update(status='rejected', rejection_reason=reason)
            messages.warning(request, f'Rejected {target_sheets.count()} scoresheet(s) and returned to lecturer.')
            
        return redirect('portal:scoresheet')

    context = {
        'scoresheets': scoresheet_qs,
        'role_label': role_label,
        'target_status': target_status,
        'page_title': 'Result Moderation',
    }
    return render(request, 'results/scoresheet_moderation.html', context)


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('student')
def student_fees_view(request):
    user = request.user
    
    # Enforce Matric Number Dependency — must have matric before paying fees
    try:
        _profile = user.student_profile
        if not _profile.matriculation_number:
            raise AttributeError
    except (StudentProfile.DoesNotExist, AttributeError):
        messages.error(request, 'You must complete clearance and receive your matric number before paying school fees.')
        return redirect('clearance:dashboard')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'generate_invoice':
            amount = request.POST.get('amount')
            if amount:
                try:
                    amount = float(amount)
                    if amount > 0:
                        import uuid
                        ref = str(uuid.uuid4())[:12].upper()
                        FeePayment.objects.create(
                            student=user,
                            amount=amount,
                            payment_method='online',
                            transaction_reference=f"RRR-{ref}",
                            purpose='tuition_fee',
                            status='pending'
                        )
                        messages.success(request, f'Invoice RRR-{ref} generated successfully.')
                except ValueError:
                    messages.error(request, 'Invalid amount provided.')
        elif action == 'verify_payment':
            payment_id = request.POST.get('payment_id')
            try:
                p = FeePayment.objects.get(pk=payment_id, student=user)
                if p.status == 'pending':
                    p.status = 'completed'
                    p.verified_at = timezone.now()
                    p.save(update_fields=['status', 'verified_at'])
                    _audit(request, 'FEE_PAYMENT', 'PAYMENT', p.pk, details=f'Payment {p.transaction_reference} mock verified')
                    try:
                        profile = user.student_profile
                        profile.total_fees_paid += p.amount
                        profile.save(update_fields=['total_fees_paid'])
                    except Exception:
                        pass
                    messages.success(request, 'Payment verified successfully!')
            except FeePayment.DoesNotExist:
                messages.error(request, 'Payment record not found.')
        return redirect('portal:student_fees')

    payments = FeePayment.objects.filter(student=user).order_by('-payment_date')
    total_paid = sum(p.amount for p in payments if p.status == 'completed')

    # Use dynamic FeeItems set by bursary (fallback to old FeeStructure)
    from fees.models import FeeItem
    fee_items = FeeItem.get_active_items()

    if fee_items.exists():
        total_due = FeeItem.get_total()
    else:
        fee_structure = FeeStructure.objects.filter(is_active=True).first()
        total_due = fee_structure.total_fee if fee_structure else 0

    balance = max(0, total_due - total_paid)
    
    # Pass pending invoice so we can show payment button
    pending_invoice = payments.filter(status='pending').first()

    return render(request, 'fees/student_fees.html', {
        'payments': payments,
        'fee_items': fee_items,
        'total_paid': total_paid,
        'total_due': total_due,
        'balance': balance,
        'pending_invoice': pending_invoice,
        'fee_details': {'total_paid': total_paid, 'total_due': total_due, 'balance': balance},
        'page_title': 'Fee Payment',
    })


@login_required_portal
def receipt_detail_view(request, payment_id):
    """
    Dynamic print-ready receipt for Yar'yaya College.
    Fetches student info and fee breakdown.
    """
    payment = get_object_or_404(FeePayment, pk=payment_id)
    # Security: student can only see their own receipt
    if request.user.role == 'student' and payment.student != request.user:
        return HttpResponse("Unauthorized", status=403)
        
    student = payment.student
    profile = getattr(student, 'student_profile', None)
    
    # Get fee structure for this student's level/program to show breakdown
    fee_structure = None
    if profile:
        fee_structure = FeeStructure.objects.filter(
            level=profile.level,
            program=profile.department_code,
            academic_year='2024/2025', # Current session
            is_active=True
        ).first()
    
    if not fee_structure:
        fee_structure = FeeStructure.objects.filter(is_active=True).first()

    # Dynamic Items Breakdown from FeeStructure
    fee_items = []
    if fee_structure:
        mapping = [
            ('tuition_fee_per_unit', 'Tuition Fee (Per Unit)'),
            ('acceptance_fee', 'Acceptance Fee'),
            ('registration_fee', 'Undergraduate Registration'),
            ('examination_fee', 'Examination Fee'),
            ('library_fee', 'Library Fee'),
            ('medical_fee', 'Medical Laboratory Test'),
            ('sports_fee', 'Sports & Games'),
            ('development_fee', 'Development Levy'),
            ('hostel_fee', 'Hostel Accommodation'),
            ('other_fees', 'Other Mandatory Charges'),
        ]
        
        for field, label in mapping:
            val = getattr(fee_structure, field, 0)
            if val and val > 0:
                fee_items.append({'name': label, 'amount': val})

    return render(request, 'fees/receipt_detail.html', {
        'payment': payment,
        'profile': profile,
        'fee_items': fee_items,
        'college_full_name': "Yar'yaya College of Health Science and Technology Sanga",
        'college_short_name': "YCHST SANGA",
        'page_title': 'Payment Receipt',
    })


@login_required_portal
@role_required('student')
def session_receipt_view(request):
    """
    Consolidated session receipt for all cleared payments.
    """
    user = request.user
    payments = FeePayment.objects.filter(student=user, status='completed').order_by('-payment_date')
    
    if not payments.exists():
        messages.warning(request, "No cleared payments found to generate a receipt.")
        return redirect('portal:my_documents')

    profile = getattr(user, 'student_profile', None)
    total_paid = sum(p.amount for p in payments)
    
    # Use the first payment's timestamp/ref or common session info
    main_payment = payments.first()
    
    # Get fee structure for breakdown
    fee_structure = None
    if profile:
        fee_structure = FeeStructure.objects.filter(
            level=profile.level,
            program=profile.department_code,
            academic_year='2024/2025',
            is_active=True
        ).first()

    if not fee_structure:
        fee_structure = FeeStructure.objects.filter(is_active=True).first()

    fee_items = []
    if fee_structure:
        mapping = [
            ('tuition_fee_per_unit', 'Tuition Fee (Per Unit)'),
            ('acceptance_fee', 'Acceptance Fee'),
            ('registration_fee', 'Undergraduate Registration'),
            ('examination_fee', 'Examination Fee'),
            ('library_fee', 'Library Fee'),
            ('medical_fee', 'Medical Laboratory Test'),
            ('sports_fee', 'Sports & Games'),
            ('development_fee', 'Development Levy'),
            ('hostel_fee', 'Hostel Accommodation'),
            ('other_fees', 'Other Mandatory Charges'),
        ]
        
        for field, label in mapping:
            val = getattr(fee_structure, field, 0)
            if val and val > 0:
                fee_items.append({'name': label, 'amount': val})

    # Add transaction references as a comma-separated list
    trans_ids = ", ".join([p.transaction_reference for p in payments])

    return render(request, 'fees/receipt_detail.html', {
        'payment': main_payment,
        'payments': payments,
        'profile': profile,
        'fee_items': fee_items,
        'total_paid': total_paid,
        'trans_ids': trans_ids,
        'college_full_name': "Yar'yaya College of Health Science and Technology Sanga",
        'college_short_name': "YCHST SANGA",
        'page_title': 'Session Payment Receipt',
    })
#     user = request.user
    
#     payments = FeePayment.objects.filter(student=user).order_by('-payment_date')
#     fee_structure = FeeStructure.objects.filter(is_active=True).first()
#     total_paid = sum(p.amount for p in payments if p.status == 'completed')
#     total_due = fee_structure.total_fee if fee_structure else 0
#     balance = max(0, total_due - total_paid)
    
#     # Create PDF
#     response = HttpResponse(content_type='application/pdf')
#     response['Content-Disposition'] = 'attachment; filename="fee_receipt.pdf"'
    
#     doc = SimpleDocTemplate(response, pagesize=letter)
#     styles = getSampleStyleSheet()
    
#     story = []
#     story.append(Paragraph("Fee Receipt", styles['Title']))
#     story.append(Spacer(1, 12))
#     story.append(Paragraph(f"Student: {user.get_full_name()}", styles['Normal']))
#     story.append(Paragraph(f"Matric Number: {user.username}", styles['Normal']))
#     story.append(Spacer(1, 12))
#     story.append(Paragraph(f"Total Due: ₦{total_due:,.2f}", styles['Normal']))
#     story.append(Paragraph(f"Total Paid: ₦{total_paid:,.2f}", styles['Normal']))
#     story.append(Paragraph(f"Outstanding Balance: ₦{balance:,.2f}", styles['Normal']))
#     story.append(Spacer(1, 12))
#     story.append(Paragraph("Payment History:", styles['Heading2']))
    
#     for payment in payments:
#         status = "Completed" if payment.status == 'completed' else "Pending"
#         story.append(Paragraph(f"{payment.payment_date.strftime('%Y-%m-%d')}: ₦{payment.amount:,.2f} - {status} ({payment.transaction_reference})", styles['Normal']))
    
#     doc.build(story)
#     return response


@login_required_portal
@role_required('bursary', 'registrar', 'ict_director', 'director', 'provost')
def fee_management_view(request):
    """
    Bursary fee management — two sections:
    1. Fee Items (add/update/delete fee line items with fixed amounts)
    2. Payment Ledger (view all student payments)
    """
    from fees.models import FeeItem

    # ── Fee Item CRUD ────────────────────────────────────────────────────
    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'add_fee_item':
            name = request.POST.get('name', '').strip()
            amount = request.POST.get('amount', '0')
            category = request.POST.get('category', 'other')
            description = request.POST.get('description', '').strip()
            academic_year = request.POST.get('academic_year', '2025/2026')
            is_mandatory = request.POST.get('is_mandatory') == 'on'
            order = request.POST.get('display_order', '0')

            if name and float(amount) > 0:
                FeeItem.objects.create(
                    name=name,
                    amount=float(amount),
                    category=category,
                    description=description,
                    academic_year=academic_year,
                    is_mandatory=is_mandatory,
                    display_order=int(order) if order else 0,
                    created_by=request.user,
                )
                _audit(request, 'FEE_ITEM', 'ADD', request.user.pk,
                       details=f'Added fee item: {name} = N{amount}')
                messages.success(request, f'Fee item "{name}" added successfully.')
            else:
                messages.error(request, 'Please provide a valid name and amount.')

        elif action == 'update_fee_item':
            item_id = request.POST.get('item_id')
            try:
                item = FeeItem.objects.get(pk=item_id)
                item.name = request.POST.get('name', item.name).strip()
                item.amount = float(request.POST.get('amount', item.amount))
                item.category = request.POST.get('category', item.category)
                item.description = request.POST.get('description', '').strip()
                item.is_mandatory = request.POST.get('is_mandatory') == 'on'
                item.is_active = request.POST.get('is_active') == 'on'
                item.display_order = int(request.POST.get('display_order', 0))
                item.save()
                _audit(request, 'FEE_ITEM', 'UPDATE', item.pk,
                       details=f'Updated fee item: {item.name} = N{item.amount}')
                messages.success(request, f'Fee item "{item.name}" updated.')
            except FeeItem.DoesNotExist:
                messages.error(request, 'Fee item not found.')

        elif action == 'delete_fee_item':
            item_id = request.POST.get('item_id')
            try:
                item = FeeItem.objects.get(pk=item_id)
                item_name = item.name
                item.delete()
                _audit(request, 'FEE_ITEM', 'DELETE', request.user.pk,
                       details=f'Deleted fee item: {item_name}')
                messages.success(request, f'Fee item "{item_name}" deleted.')
            except FeeItem.DoesNotExist:
                messages.error(request, 'Fee item not found.')

        return redirect('portal:fee_management')

    # ── Query fee items ──────────────────────────────────────────────────
    fee_items = FeeItem.objects.all().order_by('display_order', 'category', 'name')
    fee_items_total = FeeItem.get_total()
    active_items_count = FeeItem.objects.filter(is_active=True).count()

    # ── Query payments ledger ────────────────────────────────────────────
    search = request.GET.get('search', '')
    status_filter = request.GET.get('status', '')

    payments = FeePayment.objects.select_related('student', 'verified_by').order_by('-payment_date')
    if search:
        payments = payments.filter(
            Q(student__username__icontains=search) |
            Q(student__first_name__icontains=search) |
            Q(student__last_name__icontains=search) |
            Q(transaction_reference__icontains=search)
        )
    if status_filter:
        payments = payments.filter(status=status_filter)

    total_paid = FeePayment.objects.filter(status='completed').aggregate(Sum('amount'))['amount__sum'] or 0
    total_pending = FeePayment.objects.filter(status='pending').aggregate(Sum('amount'))['amount__sum'] or 0
    total_failed = FeePayment.objects.filter(status='failed').aggregate(Sum('amount'))['amount__sum'] or 0

    return render(request, 'fees/fee_management.html', {
        'fee_items': fee_items,
        'fee_items_total': fee_items_total,
        'active_items_count': active_items_count,
        'category_choices': FeeItem.CATEGORY_CHOICES,
        'payments': payments[:100],
        'search': search,
        'status_filter': status_filter,
        'total_paid': total_paid,
        'total_pending': total_pending,
        'total_failed': total_failed,
        'page_title': 'Fee Management & Bursary Overview',
    })


# ---------------------------------------------------------------------------
# User Management (ICT Director)
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('ict_director', 'director')
def user_management_view(request):
    search = request.GET.get('search', '')
    role_filter = request.GET.get('role', '')

    users = User.objects.all().select_related('student_profile').order_by('role', 'last_name')
    if search:
        users = users.filter(
            Q(username__icontains=search) |
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(email__icontains=search) |
            Q(student_profile__matriculation_number__icontains=search)
        )
    if role_filter:
        users = users.filter(role=role_filter)

    if request.method == 'POST':
        action = request.POST.get('action')
        user_id = request.POST.get('user_id')
        try:
            target = User.objects.get(pk=user_id)
            if action == 'lock':
                from datetime import timedelta
                target.account_locked_until = timezone.now() + timedelta(hours=24)
                target.save(update_fields=['account_locked_until'])
                _audit(request, 'ACCOUNT_LOCK', 'USER', user_id,
                       details=f'Account locked by ICT Director')
                messages.success(request, f'{target.username} account locked.')
            elif action == 'unlock':
                target.account_locked_until = None
                target.failed_login_attempts = 0
                target.save(update_fields=['account_locked_until', 'failed_login_attempts'])
                _audit(request, 'ACCOUNT_UNLOCK', 'USER', user_id,
                       details=f'Account unlocked by ICT Director')
                messages.success(request, f'{target.username} account unlocked.')
            elif action == 'change_role':
                old_role = target.role
                new_role = request.POST.get('new_role')
                if new_role in dict(User.ROLE_CHOICES):
                    target.role = new_role
                    target.save(update_fields=['role'])
                    _audit(request, 'ROLE_CHANGE', 'USER', user_id,
                           old_value={'role': old_role},
                           new_value={'role': new_role},
                           details=f'Role changed: {old_role} → {new_role}')
                    messages.success(request, f'Role updated for {target.username}.')
        except User.DoesNotExist:
            messages.error(request, 'User not found.')
        return redirect('portal:user_management')

    return render(request, 'users/user_management.html', {
        'users': users,
        'search': search,
        'role_filter': role_filter,
        'role_choices': User.ROLE_CHOICES,
        'page_title': 'User Management',
    })


# ---------------------------------------------------------------------------
# Audit Logs (ICT Director)
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('ict_director')
def audit_logs_view(request):
    search = request.GET.get('search', '')
    action_filter = request.GET.get('action', '')
    logs = AuditLog.objects.select_related('user').order_by('-created_at')

    if search:
        logs = logs.filter(
            Q(user__username__icontains=search) |
            Q(details__icontains=search) |
            Q(ip_address__icontains=search)
        )
    if action_filter:
        logs = logs.filter(action=action_filter)

    return render(request, 'ict/audit_logs.html', {
        'logs': logs[:200],
        'action_choices': AuditLog.ACTION_CHOICES,
        'search': search,
        'action_filter': action_filter,
        'page_title': 'Audit Logs',
    })


# ---------------------------------------------------------------------------
# OTP Manager (ICT Director)
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('ict_director')
def otp_manager_view(request):
    pending_otps = OTPRecord.objects.filter(
        status='generated'
    ).select_related('user').order_by('-generated_at')

    recent_otps = OTPRecord.objects.select_related('user').order_by('-generated_at')[:20]

    if request.method == 'POST':
        action = request.POST.get('action')
        otp_id = request.POST.get('otp_id')
        try:
            otp = OTPRecord.objects.get(pk=otp_id)
            if action == 'invalidate':
                otp.status = 'expired'
                otp.save(update_fields=['status'])
                _audit(request, 'OTP_USE', 'OTP', otp_id,
                       details='OTP manually invalidated by ICT Director')
                messages.success(request, 'OTP invalidated.')
        except OTPRecord.DoesNotExist:
            messages.error(request, 'OTP record not found.')
        return redirect('portal:otp_manager')

    return render(request, 'ict/otp_manager.html', {
        'pending_otps': pending_otps,
        'recent_otps': recent_otps,
        'page_title': 'OTP Manager',
    })


# ---------------------------------------------------------------------------
# General Permissions
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('registrar', 'ict_director')
def general_permissions_view(request):
    permissions = GeneralPermission.objects.select_related(
        'granted_to', 'granted_by'
    ).order_by('-created_at')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            permission_type = request.POST.get('permission_type')
            user_id = request.POST.get('user_id')
            reason = request.POST.get('reason', '')
            valid_from = request.POST.get('valid_from')
            valid_to = request.POST.get('valid_to')
            try:
                target = User.objects.get(pk=user_id)
                from django.utils.dateparse import parse_datetime
                perm = GeneralPermission.objects.create(
                    permission_type=permission_type,
                    granted_to=target,
                    granted_by=request.user,
                    reason=reason,
                    valid_from=parse_datetime(valid_from) or timezone.now(),
                    valid_to=parse_datetime(valid_to) or timezone.now(),
                )
                _audit(request, 'PERMISSION_GRANT', 'PERMISSION', perm.pk,
                       new_value={'type': permission_type, 'user': target.username},
                       details=f'Permission "{permission_type}" granted to {target.username}')
                messages.success(request, f'Permission granted to {target.username}.')
            except User.DoesNotExist:
                messages.error(request, 'User not found.')
        elif action == 'revoke':
            perm_id = request.POST.get('perm_id')
            try:
                perm = GeneralPermission.objects.get(pk=perm_id)
                perm.is_active = False
                perm.save(update_fields=['is_active'])
                messages.warning(request, 'Permission revoked.')
            except GeneralPermission.DoesNotExist:
                messages.error(request, 'Permission not found.')
        return redirect('portal:general_permissions')

    all_users = User.objects.filter(is_active=True).order_by('last_name', 'first_name')
    return render(request, 'permissions/general_permissions.html', {
        'permissions': permissions,
        'all_users': all_users,
        'permission_types': GeneralPermission.PERMISSION_TYPE_CHOICES,
        'page_title': 'General Permissions',
    })


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------
@login_required_portal
def announcements_view(request):
    user = request.user
    notices = Notification.objects.filter(
        Q(recipient=user) | Q(recipient_role=user.role) | Q(recipient_role='all')
    ).order_by('-created_at')

    if request.method == 'POST' and request.POST.get('action') == 'mark_read':
        notice_id = request.POST.get('notice_id')
        try:
            n = Notification.objects.get(pk=notice_id, recipient=user)
            n.is_read = True
            n.read_at = timezone.now()
            n.save(update_fields=['is_read', 'read_at'])
        except Notification.DoesNotExist:
            pass
        return JsonResponse({'status': 'ok'})

    return render(request, 'announcements/list.html', {
        'notices': notices,
        'page_title': 'Announcements',
    })


# ---------------------------------------------------------------------------
# Reports (HOD / Dean / VC)
# ---------------------------------------------------------------------------
@login_required_portal
@role_required('hod', 'dean_students_affairs', 'deputy_dean_students_affairs', 'director', 'registrar', 'provost')
def reports_view(request):
    context = {
        'total_students': StudentProfile.objects.count(),
        'total_staff': StaffProfile.objects.count(),
        'total_courses': Course.objects.filter(is_active=True).count(),
        'results_by_grade': Result.objects.values('grade').annotate(count=Count('grade')).order_by('grade'),
        'page_title': 'Reports',
    }
    return render(request, 'reports/reports.html', context)


# ---------------------------------------------------------------------------
# Error Pages
# ---------------------------------------------------------------------------
def error_403(request, exception=None):
    return render(request, 'errors/403.html', status=403)


def error_404(request, exception=None):
    return render(request, 'errors/404.html', status=404)


def error_500(request):
    return render(request, 'errors/500.html', status=500)


# ---------------------------------------------------------------------------
# NSUK-matching Student Pages
# ---------------------------------------------------------------------------

@login_required_portal
@role_required('student')
def timetable_view(request):
    """Student timetable / class schedule."""
    user = request.user
    fees_cleared, total_paid, total_due = _check_student_clearance(user)
    
    if not fees_cleared:
        messages.warning(request, 'Timetable access is restricted until fees are cleared.')
        return redirect('portal:dashboard')

    # Fetch offerings for the student's level/dept that have schedule info
    registrations = CourseRegistration.objects.filter(
        student=user, academic_year='2024/2025'
    ).values_list('course_code', flat=True)

    offerings = CourseOffering.objects.filter(
        course__course_code__in=registrations,
        is_active=True,
    ).select_related('course').order_by('day_of_week', 'start_time')

    return render(request, 'students/timetable.html', {
        'offerings': offerings,
        'page_title': 'My Timetable',
    })


@login_required_portal
@role_required('student')
def change_programme_view(request):
    """Change of programme request."""
    user = request.user
    try:
        profile = user.student_profile
    except Exception:
        profile = None

    submitted = False
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        new_programme = request.POST.get('new_programme', '').strip()
        if reason and new_programme:
            _audit(request, 'CHANGE_PROGRAMME_REQUEST', 'STUDENT', user.pk,
                   new_value={'new_programme': new_programme},
                   details=f'Change of programme to {new_programme}: {reason}')
            Notification.objects.create(
                title=f'Change of Programme Request — {user.get_full_name()}',
                message=f'Student {user.username} requests a change to {new_programme}. Reason: {reason}',
                recipient_role='registrar',
                notification_type='info',
            )
            messages.success(request, 'Your change of programme request has been submitted to the Registrar.')
            submitted = True

    return render(request, 'students/change_programme.html', {
        'profile': profile,
        'submitted': submitted,
        'page_title': 'Change of Programme',
    })


@login_required_portal
@role_required('student')
def my_documents_view(request):
    """Print-ready documents: course form, exam card, payment receipt."""
    user = request.user
    fees_cleared, total_paid, total_due = _check_student_clearance(user)
    
    payments = FeePayment.objects.filter(student=user, status='completed').order_by('-payment_date')
    
    # If fees aren't cleared, they only see payments (receipts)
    registrations = []
    if fees_cleared:
        registrations = CourseRegistration.objects.filter(
            student=user, academic_year='2024/2025'
        )

    return render(request, 'students/my_documents.html', {
        'registrations': registrations,
        'payments': payments,
        'fees_cleared': fees_cleared,
        'total_paid': total_paid,
        'total_due': total_due,
        'page_title': 'My Documents',
    })


@login_required_portal
@role_required('student')
def clearance_view(request):
    """Redirect to the new clearance module."""
    return redirect('clearance:dashboard')


@login_required_portal
def support_view(request):
    """Help desk / support page."""
    submitted = False
    if request.method == 'POST':
        subject = request.POST.get('subject', '').strip()
        body = request.POST.get('body', '').strip()
        if subject and body:
            _audit(request, 'SUPPORT_TICKET', 'SUPPORT', request.user.pk,
                   new_value={'subject': subject},
                   details=f'Support ticket: {subject}')
            Notification.objects.create(
                title=f'Support Ticket: {subject}',
                message=f'From {request.user.username}: {body}',
                recipient_role='ict_director',
                notification_type='info',
            )
            messages.success(request, 'Your support request has been sent. We will respond shortly.')
            submitted = True

    return render(request, 'students/support.html', {
        'submitted': submitted,
        'page_title': 'Help & Support',
    })


@login_required_portal
@role_required('student')
def other_payment_view(request):
    """Other (non-tuition) payments: accommodation, late registration, etc."""
    from students.models import CourseRegistration as CR
    user = request.user

    if request.method == 'POST':
        payment_type = request.POST.get('payment_type', '')
        amount = request.POST.get('amount', '')
        try:
            amount = float(amount)
            if amount > 0 and payment_type:
                import uuid
                ref = str(uuid.uuid4())[:12].upper()
                from fees.models import OtherPayment
                OtherPayment.objects.create(
                    student=user,
                    payment_type=payment_type,
                    amount=amount,
                    transaction_reference=f'OTH-{ref}',
                    status='pending',
                )
                messages.success(request, f'Invoice OTH-{ref} generated for {payment_type}.')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid amount.')
        return redirect('portal:other_payment')

    from fees.models import OtherPayment
    other_payments = OtherPayment.objects.filter(student=user).order_by('-payment_date')
    payment_types = OtherPayment.PAYMENT_TYPE_CHOICES

    return render(request, 'students/other_payment.html', {
        'other_payments': other_payments,
        'payment_types': payment_types,
        'page_title': 'Other Payments',
    })


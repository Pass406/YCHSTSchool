from django.urls import path
from . import views

app_name = 'portal'

urlpatterns = [
    # Auth
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('get-password/', views.get_password_view, name='get_password'),

    # Dashboard
    path('dashboard/', views.dashboard_view, name='dashboard'),

    # Profile
    path('profile/', views.profile_view, name='profile'),
    path('biodata/complete/', views.complete_biodata, name='complete_biodata'),
    path('biodata/update/', views.update_biodata, name='update_biodata'),
    path('password/change/', views.change_password, name='change_password'),

    # Courses
    path('courses/register/', views.course_registration_view, name='course_registration'),
    path('courses/approvals/', views.registration_approval_view, name='registration_approvals'),
    path('courses/my-courses/', views.my_courses_view, name='my_courses'),
    path('courses/offerings/', views.manage_offerings_view, name='manage_offerings'),
    path('courses/management/', views.manage_courses_view, name='manage_courses'),

    # Results
    path('results/', views.student_results_view, name='student_results'),
    path('results/score-entry/', views.score_entry_view, name='score_entry'),
    path('results/scoresheet/', views.scoresheet_view, name='scoresheet'),

    # Fees
    path('fees/', views.student_fees_view, name='student_fees'),
    path('fees/management/', views.fee_management_view, name='fee_management'),
    path('fees/receipt/session/', views.session_receipt_view, name='session_receipt'),
    path('fees/receipt/<int:payment_id>/', views.receipt_detail_view, name='receipt_detail'),

    # User Management (ICT)
    path('users/', views.user_management_view, name='user_management'),

    # ICT Tools
    path('audit-logs/', views.audit_logs_view, name='audit_logs'),
    path('otp-manager/', views.otp_manager_view, name='otp_manager'),

    # Permissions
    path('permissions/', views.general_permissions_view, name='general_permissions'),

    # Announcements
    path('announcements/', views.announcements_view, name='announcements'),

    # Reports
    path('reports/', views.reports_view, name='reports'),

    # ── NSUK-matching student pages ──────────────────────────────
    path('timetable/', views.timetable_view, name='timetable'),
    path('change-programme/', views.change_programme_view, name='change_programme'),
    path('my-documents/', views.my_documents_view, name='my_documents'),
    path('clearance/', views.clearance_view, name='clearance'),
    path('support/', views.support_view, name='support'),
    path('other-payment/', views.other_payment_view, name='other_payment'),
]

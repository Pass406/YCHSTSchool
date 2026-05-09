from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from portal import views as portal_views

handler403 = 'portal.views.error_403'
handler404 = 'portal.views.error_404'
handler500 = 'portal.views.error_500'

urlpatterns = [
    # Root → redirect to dashboard or login
    path('', lambda request: redirect('portal:dashboard') if request.user.is_authenticated else redirect('portal:login')),
    path('login/', portal_views.login_view, name='root_login'),
    path('logout/', portal_views.logout_view, name='root_logout'),

    # Django Admin
    path('admin/', admin.site.urls),

    # Portal frontend (template-based)
    path('admissions/', include('admissions.urls')),
    path('hostel/', include('hostels.urls')),
    path('clearance/', include('clearance.urls')),
    path('', include('portal.urls')),

    # Payment gateway webhooks
    path('payments/', include('payments.urls')),

    # REST API (preserved)
    path('api/auth/', include('accounts.urls')),
    path('api/students/', include('students.urls')),
    path('api/staff/', include('staff.urls')),
    path('api/ict-director/', include('ict_director.urls')),
    path('api/director/', include('director.urls')),
    path('api/fees/', include('fees.urls')),
    path('api/courses/', include('courses.urls')),
    path('api/results/', include('results.urls')),
    path('api/notifications/', include('notifications.urls')),
    path('api/otp/', include('otp.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
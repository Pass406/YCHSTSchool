from django.db import models
from accounts.models import User
from django.utils import timezone

class Hostel(models.Model):
    """Hostel entity representing a physical building."""
    GENDER_CHOICES = [
        ('male', 'Male'),
        ('female', 'Female'),
    ]
    
    name = models.CharField(max_length=100)
    gender_category = models.CharField(max_length=10, choices=GENDER_CHOICES)
    location = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    
    # Financials
    price_per_session = models.DecimalField(max_digits=12, decimal_places=2, default=25000.00)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} ({self.get_gender_category_display()})"

class Room(models.Model):
    """A room within a hostel."""
    hostel = models.ForeignKey(Hostel, on_delete=models.CASCADE, related_name='rooms')
    room_number = models.CharField(max_length=20)
    floor = models.CharField(max_length=20, blank=True)
    capacity = models.PositiveIntegerField(default=4)
    
    def __str__(self):
        return f"{self.hostel.name} - Room {self.room_number}"
    
    @property
    def available_beds(self):
        return self.bed_spaces.filter(is_available=True).count()

class BedSpace(models.Model):
    """An individual bed space within a room."""
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='bed_spaces')
    bed_identifier = models.CharField(max_length=10, help_text="e.g. Bed A, Bed B")
    is_available = models.BooleanField(default=True)
    
    def __str__(self):
        return f"{self.room} - {self.bed_identifier}"

class HostelAllocation(models.Model):
    """Records the allocation of a bed space to a student."""
    STATUS_CHOICES = [
        ('pending_payment', 'Pending Payment'),
        ('paid', 'Paid & Allocated'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    ]
    
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='hostel_allocations')
    bed_space = models.OneToOneField(BedSpace, on_delete=models.CASCADE, related_name='allocation')
    academic_year = models.CharField(max_length=9, default='2024/2025')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending_payment')
    
    # Payment info
    payment_reference = models.CharField(max_length=50, blank=True, null=True)
    allocated_at = models.DateTimeField(auto_now_add=True)
    payment_date = models.DateTimeField(null=True, blank=True)

    # Payment gateway tracking
    GATEWAY_CHOICES = [
        ('paystack', 'Paystack'),
        ('flutterwave', 'Flutterwave'),
        ('manual', 'Manual / Offline'),
    ]
    payment_gateway = models.CharField(
        max_length=20,
        choices=GATEWAY_CHOICES,
        default='paystack',
        help_text="Payment gateway used to process this transaction",
    )
    gateway_response = models.JSONField(
        null=True,
        blank=True,
        help_text="Raw response data returned by the payment gateway",
    )
    
    def __str__(self):
        return f"{self.student.get_full_name()} - {self.bed_space}"
    
    class Meta:
        unique_together = ['student', 'academic_year'] # Student can only have one allocation per year

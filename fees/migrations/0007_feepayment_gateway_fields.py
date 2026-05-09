from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fees', '0006_feeitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='feepayment',
            name='payment_gateway',
            field=models.CharField(
                choices=[
                    ('paystack', 'Paystack'),
                    ('flutterwave', 'Flutterwave'),
                    ('manual', 'Manual / Offline'),
                ],
                default='paystack',
                help_text='Payment gateway used to process this transaction',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='feepayment',
            name='gateway_response',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Raw response data returned by the payment gateway',
            ),
        ),
    ]

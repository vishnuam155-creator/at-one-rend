# signals.py (optional — auto-create subscription on user create)

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from .models import SubscriptionPlan

User = get_user_model()

@receiver(post_save, sender=User)
def ensure_subscription_for_new_user(sender, instance, created, **kwargs):
    if created:
        # create a default basic subscription for every new user
        SubscriptionPlan.objects.create(user=instance, plan_type="basic")

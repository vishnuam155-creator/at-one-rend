from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Feedback

@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at')
    search_fields = ('name', 'message')

# if need mail and name 

    # list_display = ('name', 'email', 'created_at')
    # search_fields = ('name', 'email', 'message')

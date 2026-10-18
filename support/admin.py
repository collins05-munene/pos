from django.contrib import admin

from . import models

# Register your models here.
admin.site.register(models.TicketEvent)
admin.site.register(models.FeatureVote)
admin.site.register(models.FeatureRequest)
admin.site.register(models.HelpArticle)
admin.site.register(models.TicketAttachment)
admin.site.register(models.Ticket)
admin.site.register(models.TicketMessage)
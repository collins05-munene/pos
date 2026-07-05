from .utils import log_action, AuditAction

class AuditLogMixin:
    def form_valid(self, form):
        is_create = self.object is None
        response = super().form_valid(form)
        action = AuditAction.RECORD_CREATE if is_create else AuditAction.RECORD_UPDATE
        log_action(
            self.request.user,
             action,
             f"{self.model._meta.verbose_name.title}: {self.object}",
             self.request
        )
        return response
    
    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        label = f"{self.model._meta_verbose_name.title()}: {self.object} (id={self.object.pk})"
        response = super().delete(request, *args, **kwargs)
        log_action(request.user, AuditAction.RECORD_DELETE, label, request)
        return response
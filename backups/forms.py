from django import forms

from .models import BackupSchedule


class BackupScheduleForm(forms.ModelForm):
    class Meta:
        model = BackupSchedule
        fields = ["enabled", "frequency", "hour", "keep_last"]

    def clean_hour(self):
        h = self.cleaned_data["hour"]
        if not 0 <= h <= 23:
            raise forms.ValidationError("Hour must be between 0 and 23.")
        return h

    def clean_keep_last(self):
        n = self.cleaned_data["keep_last"]
        if not 3 <= n <= 60:
            raise forms.ValidationError("Keep between 3 and 60 backups.")
        return n
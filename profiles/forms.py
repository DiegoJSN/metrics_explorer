from django import forms
from django.utils.translation import gettext_lazy as _

# Clase para procesar los formularios de búsqueda e introducirlos en views
class SearchForm(forms.Form):
    orcid = forms.CharField(label=_("ORCID"), required=False)
    researcher_name = forms.CharField(label=_("Researcher name"), required=False)
    github_name = forms.CharField(label=_("GitHub name"), required=False)

    def clean(self):
        cleaned = super().clean()
        orcid = (cleaned.get("orcid") or "").strip()
        name = (cleaned.get("researcher_name") or "").strip()
        if not name and not orcid:
            raise forms.ValidationError("Please provide at least an name or ORCID.")
        return cleaned

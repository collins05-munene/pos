from django.http import JsonResponse
from django.shortcuts import render


def html_or_json(request, template_name, context=None, json_data=None):
    """
    Return JSON when the client explicitly requests application/json.
    Otherwise render the normal Django HTML template.
    """
    context = context or {}

    if request.headers.get("Accept") == "application/json":
        # Fall back to context if json_data is not explicitly supplied
        return JsonResponse(json_data if json_data is not None else context)

    return render(request, template_name, context)
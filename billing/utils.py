import json


def read_payload(request):
    """Accept JSON bodies and normal form posts alike. Raises ValueError on bad JSON."""
    if (request.content_type or "").startswith("application/json"):
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data
    return request.POST

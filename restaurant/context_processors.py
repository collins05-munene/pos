def restaurant(request):
    """Lets templates (the sidebar) show restaurant links only to restaurants and hotels."""
    tenant = getattr(request, "tenant", None)
    if tenant is None or not getattr(tenant, "is_hospitality", False):
        return {}
    return {"hospitality": True}

def branch_switcher(request):
    """Feeds the topbar: where am I working, and where else could I go."""
    user = getattr(request, "user", None)
    branch = getattr(request, "branch", None)
    if user is None or not user.is_authenticated or branch is None:
        return {}
    from .branches import active_branches
    others = list(active_branches(request.tenant)) if user.can_switch_branch else []
    return {"active_branch": branch, "switchable_branches": others if len(others) > 1 else []}

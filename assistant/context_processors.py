from . import conf


def assistant(request):
    return {"assistant_enabled": conf.enabled(), "assistant_name": conf.name()}

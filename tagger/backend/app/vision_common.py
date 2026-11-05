"""Shared errors and optional progress for OpenRouter image analysis."""
class VisionProviderError(RuntimeError):
    pass


def emit(callback, kind, message, data=None):
    if callback:
        try:
            callback({'kind': kind, 'message': message[:240], 'data': data or {}})
        except Exception:
            pass

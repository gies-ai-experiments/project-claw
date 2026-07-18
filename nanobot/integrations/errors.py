"""Secret-safe diagnostics shared by external service adapters."""


def safe_external_error(
    service: str, operation: str, status: int | None = None
) -> str:
    """Build an operator-safe error without response bodies, credentials, or URLs."""
    suffix = f" (HTTP {status})" if status is not None else ""
    return f"{service} {operation} failed{suffix}."

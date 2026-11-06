"""Errors raised by the mocked Northwind back-office systems."""


class ServiceError(Exception):
    """Base class for mock service errors."""


class TransientServiceError(ServiceError):
    """A retryable failure (e.g. a dropped connection or 503)."""


class RecordNotFoundError(ServiceError):
    """The requested record does not exist."""


class RequestRejectedError(ServiceError):
    """The service refused the request (validation or business rule)."""

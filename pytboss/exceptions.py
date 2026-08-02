"""Exceptions used by pytboss."""


class Error(Exception):
    """Base exception class."""


class RPCError(Error):
    """Raised when an RPC returns an error.

    :param message: The error message returned by the device.
    :param code: The error code returned by the device, when it sent one.
    """

    def __init__(self, message: str = "", code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class InvalidGrill(Error):
    """Raised when an unknown or unsupported grill is requested."""


class GrillUnavailable(Error):
    """Raised when a grill is unavailable."""


class NotConnectedError(Error):
    """Raised when there is no active connection to use."""


class Unauthorized(RPCError):
    """Raised when a request is not authorized.

    Subclasses `RPCError` so that callers already catching that keep catching
    this. It is also raised by `pytboss.auth` for rejected account
    credentials, which is not an RPC -- hence the plain `Error` message
    signature still working.
    """


class UnsupportedOperation(Error):
    """Raised when an unsupported operation is attempted."""

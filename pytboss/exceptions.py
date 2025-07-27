"""Exceptions used by pytboss."""


class Error(Exception):
    """Base exception class."""


class RPCError(Error):
    """Raised when an RPC returns an error."""


class InvalidGrill(Error):
    """Raised when an unknown or unsupported grill is requested."""


class GrillUnavailable(Error):
    """Raised when a grill is unavailable."""


class NotConnectedError(Error):
    """Raised when there is no active connection to use."""


class Unauthorized(Error):
    """Raised when an RPC is not authorized."""


class UnsupportedOperation(Error):
    """Raised when an unsupported operation is attempted."""

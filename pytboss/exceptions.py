"""Exceptions used by pytboss."""


class Error(Exception):
    """Base exception class."""


class RPCError(Error):
    """Raised when an RPC returns an error."""


class InvalidGrill(Error):
    """Raised when an unknown or unsupported grill is requested."""

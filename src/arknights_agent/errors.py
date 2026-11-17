class DomainError(Exception):
    """A bounded, expected failure safe to report to a caller."""


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class InvalidData(DomainError):
    pass

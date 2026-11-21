"""Exception types that map to The Foreman exit codes."""


class RoomError(Exception):
    exit_code = 1


class UsageError(RoomError):
    exit_code = 2


class ConfigError(UsageError):
    pass


class Refused(RoomError):
    exit_code = 3


class NotFound(RoomError):
    exit_code = 4

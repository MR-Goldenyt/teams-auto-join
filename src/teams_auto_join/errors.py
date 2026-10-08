class ConfigError(Exception):
    """Invalid or missing user input."""


class JoinError(Exception):
    """Joining failed, but trying again may help."""


class FatalJoinError(JoinError):
    """Joining failed and retrying is pointless (expired link, denied, sign-in required...)."""

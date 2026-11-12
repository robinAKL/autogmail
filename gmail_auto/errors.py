class ConfigError(Exception):
    """Setup, sign-in or saved records are wrong. The message is shown to the user."""


class ProfileError(ConfigError):
    """The profile does not meet the requirements."""


class ReplyError(ConfigError):
    """No sendable reply was produced for this letter in this round."""


class NotConnected(ConfigError):
    """This mailbox or service has not been connected yet. Not an error worth alarming the user about."""

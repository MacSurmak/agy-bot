class AntigravityError(Exception):
    """Base exception for all domain and infrastructure errors in Antigravity Telegram Core."""


class StartupValidationError(AntigravityError):
    """Raised when any startup prerequisite, binary, path, or configuration invariant fails."""


class ConfigurationError(AntigravityError):
    """Raised when static or dynamic configuration is missing a key or fails validation."""


class CryptographyError(AntigravityError):
    """Raised when Fernet key initialization, encryption, or decryption fails."""


class DatabaseIntegrityError(AntigravityError):
    """Raised when a required database record is missing or violates relational invariants."""


class KeyringInitializationError(AntigravityError):
    """Raised when isolated D-Bus or gnome-keyring-daemon initialization fails for an account."""


class AccountAuthFlowError(AntigravityError):
    """Raised when Remote SSH OAuth flow fails to capture URL or complete token storage."""


class QuotaExhaustedError(AntigravityError):
    """Raised when the assigned Google account has exhausted its model quota."""

    def __init__(self, account_slug: str, model_slug: str, details: str) -> None:
        super().__init__(
            f"Quota exhausted on account '{account_slug}' for model '{model_slug}': {details}"
        )
        self.account_slug = account_slug
        self.model_slug = model_slug
        self.details = details


class RemoteMountError(AntigravityError):
    """Raised when SSH configuration or SSHFS FUSE mount verification fails."""


class AgyProcessError(AntigravityError):
    """Raised when the agy binary crashes, violates stream-json protocol, or times out."""


class ModelCompatibilityError(AntigravityError):
    """Raised when incompatible flags (such as --effort on Claude models) are requested."""


class IpcError(AntigravityError):
    """Raised when communication over the local UNIX IPC socket fails."""


class AccessDeniedError(AntigravityError):
    """Raised when an unauthorized user attempts to interact with the bot or restricted topic."""

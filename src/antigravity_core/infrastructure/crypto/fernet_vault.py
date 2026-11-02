from cryptography.fernet import Fernet, InvalidToken

from antigravity_core.domain.exceptions import CryptographyError


class FernetVault:
    """Symmetric AES-128-CBC + HMAC-SHA256 encryption vault for database secrets."""

    def __init__(self, master_key_str: str) -> None:
        raw_key = master_key_str.strip().encode("ascii")
        try:
            self._fernet = Fernet(raw_key)
        except (ValueError, TypeError) as exc:
            raise CryptographyError(
                f"Invalid Fernet master key in static configuration: {exc}"
            ) from exc

    def encrypt_text(self, plaintext: str) -> bytes:
        if not plaintext:
            raise CryptographyError("Cannot encrypt an empty plaintext string.")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt_text(self, ciphertext: bytes) -> str:
        if not ciphertext:
            raise CryptographyError("Cannot decrypt an empty ciphertext byte string.")
        try:
            decrypted_bytes = self._fernet.decrypt(bytes(ciphertext))
        except InvalidToken as exc:
            raise CryptographyError(
                "Fernet decryption failed: token is corrupted or master key does not match."
            ) from exc
        return decrypted_bytes.decode("utf-8")

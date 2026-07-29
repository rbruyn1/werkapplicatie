"""
crypto_utils.py
─────────────────────────────────────────────────────────────────
Helper-module om het PeopleSoft-wachtwoord versleuteld op te slaan
in config.json (veld "password_enc") in plaats van in klare tekst.

Belangrijk: SHA-256 is een one-way hash en kan dus NIET gebruikt
worden om opnieuw in te loggen (PeopleSoft heeft het echte
wachtwoord nodig). Daarom gebruiken we hier symmetrische,
omkeerbare versleuteling (Fernet/AES) met een lokale geheime sleutel
(secret.key). Die key staat NIET in config.json en mag niet
gedeeld/gecommit worden.

Gebruik:
    from crypto_utils import encrypt_password, decrypt_password

    cfg["password_enc"] = encrypt_password("mijnwachtwoord")
    wachtwoord = decrypt_password(cfg["password_enc"])
"""

from __future__ import annotations
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(__file__).parent
KEY_PATH = BASE_DIR / "secret.key"


def _load_or_create_key() -> bytes:
    """Laad de lokale Fernet-key, of maak er één aan als ze nog niet bestaat."""
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()

    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    try:
        # Voorzichtigheidsmaatregel: maak het bestand enkel leesbaar/schrijfbaar
        # door de eigenaar (werkt op Linux/macOS, wordt genegeerd op Windows).
        KEY_PATH.chmod(0o600)
    except Exception:
        pass
    return key


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt_password(plain_password: str) -> str:
    """Versleutel een wachtwoord -> string klaar om in config.json te zetten."""
    if not plain_password:
        return ""
    token = _get_fernet().encrypt(plain_password.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_password(encrypted_password: str) -> str:
    """Decrypteer een versleuteld wachtwoord uit config.json."""
    if not encrypted_password:
        return ""
    try:
        plain = _get_fernet().decrypt(encrypted_password.encode("utf-8"))
        return plain.decode("utf-8")
    except (InvalidToken, ValueError):
        # Ongeldige/oude token -> behandel als leeg wachtwoord
        return ""


def get_ps_password(cfg: dict) -> str:
    """
    Haal het PeopleSoft-wachtwoord op uit een config-dict.

    Ondersteunt zowel het nieuwe versleutelde veld "password_enc"
    als (voor de overgang) het oude veld "password" in klare tekst.
    """
    enc = cfg.get("password_enc")
    if enc:
        return decrypt_password(enc)
    # Fallback voor oude config-bestanden die nog "password" in klare tekst hebben
    return cfg.get("password", "")

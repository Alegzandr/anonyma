"""French, value-free wording of unexpected errors for users (issues, CLI messages).

Exception messages may quote a file name or a value: they are never shown. The
exception type is no help to a user either (« BadZipFile »): it goes to the log only.
"""

from __future__ import annotations

import ssl
import zipfile
import zlib
from pathlib import Path

#: Reason given when nothing more precise is known.
GENERIC_REASON = "fichier endommagé ou de structure inattendue"
EMPTY_REASON = "fichier vide"


def _is_empty(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size == 0
    except OSError:
        return False


def failure_reason(exc: BaseException, source: Path | None = None) -> str:
    """Why reading or writing a file failed, in French (« fichier vide »...)."""
    if _is_empty(source):
        return EMPTY_REASON
    if isinstance(exc, MemoryError):
        return "mémoire insuffisante"
    if isinstance(exc, PermissionError):
        return "accès refusé, fichier protégé ou ouvert dans un autre programme"
    if isinstance(exc, (zipfile.BadZipFile, zlib.error, EOFError)):
        return "fichier endommagé ou tronqué"
    if isinstance(exc, UnicodeError):
        return "encodage des caractères non reconnu"
    if isinstance(exc, TimeoutError):
        return "délai dépassé"
    if isinstance(exc, OSError):
        return "lecture ou écriture sur le disque impossible"
    return GENERIC_REASON


def network_reason(exc: BaseException) -> str:
    """Why a network operation (update, model download) failed, in French."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = {type(e).__name__ for e in chain}
    if "ProxyError" in names:
        return (
            "proxy injoignable ou refusé : vérifiez son adresse et, s'il en demande, "
            "l'identifiant et le mot de passe"
        )
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain):
        return "certificat du serveur non reconnu par ce poste"
    if names & {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}:
        return "délai de connexion dépassé"
    if names & {"ConnectError", "ReadError", "WriteError", "RemoteProtocolError"}:
        return "connexion impossible ou interrompue"
    if "HTTPStatusError" in names:
        return "réponse refusée par le serveur"
    if isinstance(exc, TimeoutError):
        return "délai de connexion dépassé"
    if isinstance(exc, ConnectionError):
        return "connexion impossible ou interrompue"
    if isinstance(exc, PermissionError):
        return "accès refusé"
    if isinstance(exc, OSError):
        return "erreur réseau ou disque"
    return "erreur inattendue"


__all__ = ["EMPTY_REASON", "GENERIC_REASON", "failure_reason", "network_reason"]

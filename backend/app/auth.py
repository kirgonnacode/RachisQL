
import hashlib
import hmac
from pathlib import Path
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import TOKENS_FILE
from .logging_config import logger

bearer_scheme = HTTPBearer(auto_error=False)


def _load_tokens() -> dict[str, str]:
    path = Path(TOKENS_FILE)
    if not path.exists():
        raise RuntimeError(
            f"Файл с токенами '{TOKENS_FILE}' не найден - без него"
            f"авторизоваться на /ask и /ask/image не получится, приложение не "
            f"должно стартовать в таком состоянии. Сгенерируй токен "
            f"через scripts/generate_token.py."
        )

    tokens: dict[str, str] = {}
    for line_num, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise RuntimeError(f"{TOKENS_FILE}:{line_num} - неверный формат, ожидалось 'label:hash'")
        label, token_hash = line.split(":", 1)
        label, token_hash = label.strip(), token_hash.strip().lower()
        if not label or not token_hash:
            raise RuntimeError(f"{TOKENS_FILE}:{line_num} - пустой label или hash")
        tokens[token_hash] = label

    if not tokens:
        raise RuntimeError(
            f"{TOKENS_FILE} пустой или содержит только комментарии - ни одного "
            f"токена не задано, все запросы к /ask будут отклонены как 401"
        )

    return tokens


_TOKENS_BY_HASH = _load_tokens()
logger.info("Загружено токенов: %d (%s)", len(_TOKENS_BY_HASH), ", ".join(_TOKENS_BY_HASH.values()))


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _verify_token(raw_token: str) -> str | None:
    candidate_hash = _hash_token(raw_token)
    for known_hash, label in _TOKENS_BY_HASH.items():
        if hmac.compare_digest(candidate_hash, known_hash):
            return label
    return None


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    if credentials is None or not credentials.credentials:
        logger.warning("Запрос без Authorization-заголовка отклонён")
        raise HTTPException(
            status_code=401,
            detail="Требуется Bearer-токен в заголовке Authorization",
            headers={"WWW-Authenticate": "Bearer"},
        )

    label = _verify_token(credentials.credentials)
    if label is None:
        logger.warning("Запрос с невалидным токеном отклонён")
        raise HTTPException(
            status_code=401,
            detail="Невалидный токен",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return label

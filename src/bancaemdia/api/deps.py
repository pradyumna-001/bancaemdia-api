from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.db.session import get_db
from bancaemdia.domain.registros import Usuario
from bancaemdia.repositories.usuario_repo import UsuarioRepo

security = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    _: HTTPAuthorizationCredentials | None = Security(security),
    session: AsyncSession = Depends(get_db),
) -> Usuario:
    usuario_id = getattr(request.state, "usuario_id", None)
    usuario = None if usuario_id is None else await UsuarioRepo().get_by_id(session, usuario_id)
    # O token não sabe que a conta foi desativada depois de emitido: a conta é conferida a cada pedido.
    if usuario is None or not usuario.ativo:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return usuario

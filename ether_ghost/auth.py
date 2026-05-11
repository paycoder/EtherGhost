import secrets
import typing as t

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)


async def require_auth(
    request: Request,
    credentials: t.Union[HTTPBasicCredentials, None] = Depends(security),
):
    if request.app.state.auth_username is None:
        return
    username = request.app.state.auth_username
    if credentials is None:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    if not secrets.compare_digest(credentials.username, username):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    if not secrets.compare_digest(
        credentials.password, request.app.state.auth_password
    ):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})

from fastapi.security import HTTPBearer

security = HTTPBearer(auto_error=False)

# TODO: Implement get_current_user when auth is added
# async def get_current_user(...):
#     ...

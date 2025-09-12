current_permissions = set()


def set_current_permissions(perms: set):
    global current_permissions
    current_permissions = perms


def require_permission(permission: str):
    if permission not in current_permissions:
        raise PermissionError(f"Нет разрешения: {permission}")

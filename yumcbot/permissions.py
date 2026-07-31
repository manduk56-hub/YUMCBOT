"""운영자 판정. 서버 관리 권한이 있거나 OPERATOR_ROLE_IDS 역할을 가진 사람이다.

역할 ID를 환경변수로 받는 이유는 서버마다 다르고 개발용 서버에서는 비워두기
때문이다. 비어 있으면 관리 권한만으로 판정한다.
"""

from __future__ import annotations

import os

import discord
from discord import app_commands


def operator_role_ids() -> set[int]:
    role_ids: set[int] = set()
    for value in os.getenv("OPERATOR_ROLE_IDS", "").replace(";", ",").split(","):
        value = value.strip()
        if value.isdigit():
            role_ids.add(int(value))
    return role_ids


def is_operator_user(user: discord.abc.User) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_guild):
        return True

    allowed_role_ids = operator_role_ids()
    user_roles = getattr(user, "roles", [])
    return bool(allowed_role_ids and any(role.id in allowed_role_ids for role in user_roles))


def admin_only() -> app_commands.check:
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_operator_user(interaction.user):
            return True

        raise app_commands.CheckFailure("이 명령어는 관리자 권한 또는 운영자 역할이 필요합니다.")

    return app_commands.check(predicate)

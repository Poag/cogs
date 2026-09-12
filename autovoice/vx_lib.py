"""Shared helper code for the AutoVoice cog.

Ported from PhasecoreX's ``pcx_lib.py`` (used across several PhasecoreX cogs),
trimmed down to only what AutoVoice actually uses: message deletion,
formatted setting display, and the permission-overwrite helper class.
"""

from collections.abc import Mapping
from typing import Any

import discord
from redbot.core.utils.chat_formatting import box


async def delete(message: discord.Message, *, delay: float | None = None) -> bool:
    """Attempt to delete a message.

    Returns True if successful, False otherwise.
    """
    try:
        await message.delete(delay=delay)
    except discord.NotFound:
        return True  # Already deleted
    except discord.HTTPException:
        return False
    return True


class SettingDisplay:
    """A formatted list of settings."""

    def __init__(self, header: str | None = None) -> None:
        """Init."""
        self.header = header
        self._length = 0
        self._settings: list[tuple] = []

    def add(self, setting: str, value: Any) -> None:  # noqa: ANN401
        """Add a setting."""
        setting_colon = setting + ":"
        self._settings.append((setting_colon, value))
        self._length = max(len(setting_colon), self._length)

    def raw(self) -> str:
        """Generate the raw text of this SettingDisplay, to be monospace (ini) formatted later."""
        msg = ""
        if not self._settings:
            return msg
        if self.header:
            msg += f"--- {self.header} ---\n"
        for setting in self._settings:
            msg += f"{setting[0].ljust(self._length, ' ')} [{setting[1]}]\n"
        return msg.strip()

    def display(self, *additional) -> str:  # noqa: ANN002 (Self)
        """Generate a ready-to-send formatted box of settings.

        If additional SettingDisplays are provided, merges their output into one.
        """
        msg = self.raw()
        for section in additional:
            msg += "\n\n" + section.raw()
        return box(msg, lang="ini")

    def __str__(self) -> str:
        """Generate a ready-to-send formatted box of settings."""
        return self.display()

    def __len__(self) -> int:
        """Count of how many settings there are to display."""
        return len(self._settings)


class Perms:
    """Helper class for dealing with a dictionary of discord.PermissionOverwrite."""

    def __init__(
        self,
        overwrites: (
            dict[
                discord.Role | discord.Member | discord.Object,
                discord.PermissionOverwrite,
            ]
            | None
        ) = None,
    ) -> None:
        """Init."""
        self.__overwrites: dict[
            discord.Role | discord.Member,
            discord.PermissionOverwrite,
        ] = {}
        self.__original: dict[
            discord.Role | discord.Member,
            discord.PermissionOverwrite,
        ] = {}
        if overwrites:
            for key, value in overwrites.items():
                if isinstance(key, discord.Role | discord.Member):
                    pair = value.pair()
                    self.__overwrites[key] = discord.PermissionOverwrite().from_pair(
                        *pair
                    )
                    self.__original[key] = discord.PermissionOverwrite().from_pair(
                        *pair
                    )

    def overwrite(
        self,
        target: discord.Role | discord.Member | discord.Object,
        permission_overwrite: Mapping[str, bool | None] | discord.PermissionOverwrite,
    ) -> None:
        """Set (replace) the permissions for a target."""
        if not isinstance(target, discord.Role | discord.Member):
            return
        if isinstance(permission_overwrite, discord.PermissionOverwrite):
            if permission_overwrite.is_empty():
                self.__overwrites[target] = discord.PermissionOverwrite()
                return
            self.__overwrites[target] = discord.PermissionOverwrite().from_pair(
                *permission_overwrite.pair()
            )
        else:
            self.__overwrites[target] = discord.PermissionOverwrite()
            self.update(target, permission_overwrite)

    def update(
        self,
        target: discord.Role | discord.Member,
        perm: Mapping[str, bool | None],
    ) -> None:
        """Update (merge into) the permissions for a target."""
        if target not in self.__overwrites:
            self.__overwrites[target] = discord.PermissionOverwrite()
        self.__overwrites[target].update(**perm)
        if self.__overwrites[target].is_empty():
            del self.__overwrites[target]

    @property
    def modified(self) -> bool:
        """Check if current overwrites are different from when this object was first initialized."""
        return self.__overwrites != self.__original

    @property
    def overwrites(
        self,
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite] | None:
        """Get current overwrites."""
        return self.__overwrites

from datetime import datetime, timedelta
from functools import partial
import random
from types import MethodType
from typing import Literal
import typing
import d20  # type: ignore[import-untyped]
import uuid

import discord
from discord.errors import Forbidden
import pytz
from redbot.core import commands
from redbot.core import config
from redbot.core.bot import Red
from redbot.core.config import Config

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from dogscogs.constants import COG_IDENTIFIER, TIMEZONE
from dogscogs.constants.discord.user import MAX_NAME_LENGTH as DISCORD_MAX_NICK_LENGTH
from dogscogs.constants.discord.embed import MAX_DESCRIPTION_LENGTH as DISCORD_EMBED_MAX_DESCRIPTION_LENGTH
from dogscogs.parsers.date import parse_duration_string, duration_string

scheduler = AsyncIOScheduler(timezone="US/Eastern")

COLLATERAL_LIST_SIZE = 20

RequestType = Literal["discord_deleted_user", "owner", "user", "user_strict"]

CurseType = Literal["Cursed", "Locked", "Nyamed", "Default"]

class NickQueueEntry(typing.TypedDict):
    name: str
    target_id: int
    instigator_id: typing.Optional[int]
    type: CurseType
    created_at: float
    expiration: typing.Optional[float]
    id: int

def CreateNickQueueEntry(
        *,
        name: str,
        target_id: int,
        instigator_id: typing.Optional[int] = None,
        type: CurseType,
        created_at: datetime = datetime.now(tz=TIMEZONE),
        expiration: typing.Optional[datetime] = None,
        id: int = uuid.uuid4().int,
    ) -> NickQueueEntry:
        return {
            'name': name,
            'target_id': target_id,
            'instigator_id': instigator_id,
            'type': type,
            'created_at': created_at.timestamp(),
            'expiration': expiration.timestamp() if expiration is not None else None,
            'id': id,
        }


# def NickQueueEntry(
#     name: str,
#     target_id: int,
#     instigator_id: int,
#     type="Default",
#     created_at: datetime = 0,
#     expiration: datetime = None
# ):
#     retval = {}
#     retval["name"] = name
#     retval["target_id"] = target_id
#     retval["instigator_id"] = instigator_id
#     retval["type"] = type
#     retval["created_at"] = created_at
#     retval["expiration"] = expiration
#     retval["id"] = uuid.uuid4().int
#     return retval


DEFAULT_MEMBER = {
    "nick_queue": [],
    "next_curse_available": None,
    "next_nyame_available": None,
}  # type: ignore[var-annotated]

DEFAULT_GUILD = {
    "attacker_wins_ties": True,
    "attacker_strength": "1d20",
    "defender_strength": "1d20",
    "curse_cooldown": 12 * 60 * 60,  # 12 hours
    "curse_duration": 30 * 60,  # 30 minutes
}


def bind_member(group: config.Group):

    async def is_type(self, type):
        nick_queue: typing.List[NickQueueEntry] = await self.nick_queue()
        return any(entry["type"] == type for entry in nick_queue)

    async def is_locked(self):
        return await is_type(self, "Locked")

    async def is_cursed(self):
        return await is_type(self, "Cursed")

    async def is_nyamed(self):
        return await is_type(self, "Nyamed")

    group.is_type = MethodType(is_type, group)
    group.is_locked = MethodType(is_locked, group)
    group.is_cursed = MethodType(is_cursed, group)
    group.is_nyamed = MethodType(is_nyamed, group)

    async def get_instigator_id(self, type):
        nick_queue: typing.List[NickQueueEntry] = await self.nick_queue()
        entry: typing.List[NickQueueEntry] = list(
            filter(lambda entry: entry["type"] == type, nick_queue)
        )
        if len(entry) == 0:
            return None
        else:
            return entry[0]["instigator_id"]

    async def get_locking_instigator_id(self):
        return await get_instigator_id(self, "Locked")

    async def get_cursing_instigator_id(self):
        return await get_instigator_id(self, "Cursed")

    async def get_nyaming_instigator_id(self):
        return await get_instigator_id(self, "Nyamed")

    group.get_instigator_id = MethodType(get_instigator_id, group)
    group.get_locking_instigator_id = MethodType(get_locking_instigator_id, group)
    group.get_cursing_instigator_id = MethodType(get_cursing_instigator_id, group)
    group.get_nyaming_instigator_id = MethodType(get_nyaming_instigator_id, group)

    async def get_latest(
        self, type: typing.Optional[CurseType] = None
    ) -> typing.Union[NickQueueEntry, None]:
        nick_queue: typing.List[NickQueueEntry] = await self.nick_queue()
        if len(nick_queue) == 0:
            return None
        if type is not None:
            nick_queue = list(filter(lambda entry: entry['type'] == type, nick_queue))
        if len(nick_queue) == 0:
            return None

        return max(nick_queue, key=lambda entry: entry['created_at'])

    async def get_latest_curse(self) -> typing.Union[NickQueueEntry, None]:
        return await self.get_latest("Cursed")

    async def get_latest_lock(self) -> typing.Union[NickQueueEntry, None]:
        return await self.get_latest("Locked")

    async def get_latest_nyame(self) -> typing.Union[NickQueueEntry, None]:
        return await self.get_latest("Nyamed")

    async def get_original(self) -> typing.Union[NickQueueEntry, None]:
        return await self.get_latest("Default")

    group.get_latest = MethodType(get_latest, group)
    group.get_latest_curse = MethodType(get_latest_curse, group)
    group.get_latest_lock = MethodType(get_latest_lock, group)
    group.get_latest_nyame = MethodType(get_latest_nyame, group)
    group.get_original = MethodType(get_original, group)

    async def remove_job(
        self,
        *,
        entry: typing.Optional[NickQueueEntry] = None,
        id: typing.Optional[int] = None,
    ):
        """Removes a job from the scheduler for the NickQueueEntry.

        Args:
            entry (NickQueueEntry, optional): The entry to query against. Defaults to None.
            id (int, optional): An ID for an entry to find. Defaults to None.

        Raises:
            BadArgument: If neither an entry nor id were presented, or if the ID was invalid.
        """
        if entry is None and id is None:
            raise commands.BadArgument(
                "Need to have a valid entry or id to remove a job."
            )
        elif entry is None:
            nick_queue: typing.List[NickQueueEntry] = await self.nick_queue()
            found = list(filter(lambda entry: (entry['id'] == id), nick_queue))
            if len(found) == 0:
                raise commands.BadArgument("ID was not found.")
            entry = found[0]

        job = scheduler.get_job(str(entry['id']))
        if job is not None:
            scheduler.remove_job(str(entry['id']))

    async def remove(
        self,
        *,
        type: typing.Optional[CurseType] = None,
        id: typing.Optional[int] = None,
    ):
        original_queue: typing.List[NickQueueEntry] = await self.nick_queue()

        if len(original_queue) == 0:
            return

        found = list(
            filter(lambda entry: (entry['id'] == id or entry['type'] == type), original_queue)
        )
        for entry in found:
            await self.remove_job(entry=entry)
        nick_queue = list(filter(lambda entry: entry not in found, original_queue))
        await group.nick_queue.set(nick_queue)

    async def remove_lock(self):
        return await remove(self, type="Locked")

    async def remove_curse(self):
        return await remove(self, type="Cursed")

    async def remove_original(self):
        return await remove(self, type="Default")

    async def remove_nyame(self):
        return await remove(self, type="Nyamed")

    group.remove_job = MethodType(remove_job, group)
    group.remove = MethodType(remove, group)
    group.remove_lock = MethodType(remove_lock, group)
    group.remove_curse = MethodType(remove_curse, group)
    group.remove_nyame = MethodType(remove_nyame, group)
    group.remove_original = MethodType(remove_original, group)

    async def add_entry(self, *, entry):
        nick_queue: typing.List[NickQueueEntry] = await self.nick_queue()
        nick_queue.append(entry)
        await self.nick_queue.set(nick_queue)

    group.add_entry = MethodType(add_entry, group)

    async def replace_original(self: config.Group, name: str):
        nick_queue: typing.List[NickQueueEntry] = await self.nick_queue()
        filtered = list(filter(lambda entry: entry['type'] == "Default", nick_queue))

        to_be_replaced: typing.Union[NickQueueEntry, None] = None

        if len(filtered) > 0:
            to_be_replaced = filtered[0]

        nick_queue = list(filter(lambda entry: entry['type'] != "Default", nick_queue))

        if to_be_replaced is None:
            to_be_replaced = CreateNickQueueEntry(
                name=name,
                target_id=self.identifier_data.uuid,  # type: ignore[arg-type]
                type="Default",
                created_at=datetime.fromtimestamp(0),
            )

        nick_queue.append(to_be_replaced)
        await self.nick_queue.set(nick_queue)

    group.replace_original = MethodType(replace_original, group)
    return group


class Nickname(commands.Cog):
    """
    Prevents reassigning nicknames of users until command is disabled.
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=COG_IDENTIFIER,
            force_registration=True,
        )

        self.config.register_member(**DEFAULT_MEMBER)

        self.config.register_guild(**DEFAULT_GUILD)

        scheduler.start()
        pass

    async def _get_member_ids_by_entry(
            self, 
            guild: discord.Guild, 
            *, 
            predicate : typing.Callable[[NickQueueEntry], bool] = lambda e: e['type'] != 'Default'
        ) -> typing.List[int]:
        all_members = await self.config.all_members(guild)
        return [int(key) for key, value in all_members.items() if any(predicate(entry) for entry in value['nick_queue'])]


    async def _set(self, member: discord.Member, entry: NickQueueEntry):
        guild = member.guild
        member_config = bind_member(self.config.member(member))

        original = await member_config.get_original()

        if original is None:
            await member_config.add_entry(
                entry=CreateNickQueueEntry(
                    name=member.display_name,
                    target_id=member.id,
                    type="Default",
                    created_at=datetime.fromtimestamp(0),
                )
            )

        if entry['type'] == "Locked":
            await member_config.remove_lock()
        elif entry['type'] == "Cursed":
            await member_config.remove_curse()
        elif entry['type'] == "Nyamed":
            await member_config.remove_nyame()

        await member_config.add_entry(entry=entry)
        await member.edit(
            reason=f"{member.display_name} locked nickname to {entry['name']}.",
            nick=entry['name'],
        )

        # member_ids = await self.config.guild(guild)._get_nicknamed_member_ids()
        # member_ids.append(member.id)
        # member_ids = list(set(member_ids))

        # await self.config.guild(guild).nicknamed_member_ids.set(member_ids)

        return entry

    @commands.group(aliases=["nick", "name"])
    async def nickname(self, ctx: commands.GuildContext):
        """Locks nickname changes for a user (setting them to a set nickname until unset).

        __Args__:
            ctx (commands.GuildContext): Command Context.
        """
        pass

    @nickname.command()
    @commands.is_owner()
    async def clear(
        self,
        ctx: commands.GuildContext,
        member: discord.Member,
        verbose: typing.Optional[bool] = True,
    ):
        await self.config.member(member).clear()
        if verbose:
            await ctx.send(f"Data cleared for {member.mention}.")

    @nickname.command()
    @commands.is_owner()
    async def clearall(self, ctx: commands.GuildContext):
        guild: discord.Guild = ctx.guild
        for member in guild.members:
            await self.clear(ctx, member, verbose=False)
        await ctx.send(f"Data cleared for {len(guild.members)} members.")

    @nickname.command()
    @commands.is_owner()
    async def clearcd(
        self,
        ctx: commands.GuildContext,
        member: discord.Member,
        verbose: typing.Optional[bool] = True,
    ):
        await self.config.member(member).next_curse_available.set(
            datetime.now(tz=TIMEZONE).timestamp()
        )
        if verbose:
            await ctx.send(f"Cooldown reset for {member.mention}.")

    @nickname.command()
    @commands.is_owner()
    async def clearcdsall(self, ctx: commands.GuildContext):
        guild: discord.Guild = ctx.guild
        for member in guild.members:
            await self.clearcd(ctx, member, verbose=False)
        await ctx.send(f"Cooldown reset for {len(guild.members)} members.")

    @commands.guild_only()
    @nickname.command(usage="<member>", aliases=["catify"])
    async def nyame(self, ctx: commands.GuildContext, member: discord.Member):
        """Forces a nyew nyame on a member."""
        next_nyame_available = await self.config.member(
            ctx.author
        ).next_nyame_available()

        if (
            not ctx.author.guild_permissions.manage_roles
            and next_nyame_available != None
            and next_nyame_available > datetime.now(tz=TIMEZONE).timestamp()
        ):
            await ctx.reply(
                f"{ctx.author.mention}'s nyaming power is unyavailable.  Nyext available at <t:{int(next_nyame_available)}:F>."
            )
            return

        name = ""

        i = 0

        def check_for_vowel(letter: str) -> bool:
            return letter.lower() in ["a", "e", "i", "o", "u"]

        while i < len(member.display_name) - 1:
            if member.display_name[i] == "n" or member.display_name[i] == "y":
                name += "ny"
                if not check_for_vowel(member.display_name[i + 1]):
                    name += "a"
            elif member.display_name[i] == "N" or member.display_name[i] == "Y":
                name += "Ny"
                if not check_for_vowel(member.display_name[i + 1]):
                    name += "a"
            elif member.display_name[i] == "a" and member.display_name[i + 1] == "i":
                name += "any"
                i += 1
                if i < len(member.display_name) - 1 and not check_for_vowel(
                    member.display_name[i + 1]
                ):
                    name += "a"
            elif member.display_name[i] == "A" and member.display_name[i + 1] == "i":
                name += "Any"
                i += 1
                if i < len(member.display_name) - 1 and not check_for_vowel(
                    member.display_name[i + 1]
                ):
                    name += "a"
            else:
                name += member.display_name[i]

            i += 1

        name += member.display_name[len(member.display_name) - 1]

        if name[-1] == "n":
            name += "ya"

        if name == member.display_name:
            name = (
                member.display_name.replace("pr", "purr")
                .replace("Pr", "Purr")
                .replace("me", "meow")
                .replace("Me", "Meow")
                .replace("mi", "meow")
                .replace("Mi", "Miow")
                .replace("my", "myow")
                .replace("My", "Myow")
            )

        if name == member.display_name:
            name = random.choice(
                [
                    f"🐱 {member.display_name} 🐱",
                    f"{member.display_name} Nyaa~",
                ]
            )

        name = name[:DISCORD_MAX_NICK_LENGTH]

        bot_role: discord.Role = ctx.guild.me.top_role
        target_role: discord.Role = member.top_role
        nyame_duration_secs = await self.config.guild(ctx.guild).curse_duration()
        global_nyame_cooldown = await self.config.guild(ctx.guild).curse_cooldown()
        next_available = (
            datetime.now(tz=TIMEZONE).timestamp()
            + global_nyame_cooldown
        )
        await self.config.member(ctx.author).next_nyame_available.set(next_available)

        if (
            bot_role.position < target_role.position
            or member.guild_permissions.administrator
        ):
            await self.config.member(ctx.author).next_nyame_available.set(
                datetime.now(tz=TIMEZONE).timestamp()
            )
            await ctx.reply(
                f"ERROR: Bot does nyot have permission to edit {member.display_name}'s nyicknyame. Nyour curse cooldown was refunded."
            )
            return

        expiration : datetime = datetime.now(tz=TIMEZONE) + timedelta(seconds=nyame_duration_secs)

        async def nyame_end(v: discord.Member):
            try:
                await self._unset(v, type="Nyamed")
                await ctx.send(
                    f"{ctx.author.display_name}'s Curse on {v.display_name} has ended."
                )
            except (PermissionError, Forbidden) as e:
                await self.config.member(ctx.author).next_nyame_available.set(
                    datetime.now(tz=TIMEZONE).timestamp()
                )
                await ctx.reply(
                    f"ERROR: Bot does nyot have permission to edit {member.display_name}'s nyicknyame. Nyour curse cooldown was refunded."
                )

        original_name: str = member.display_name

        entry = CreateNickQueueEntry(
            name=name,
            target_id=member.id,
            instigator_id=ctx.author.id,
            type="Nyamed",
            expiration=expiration,
        )

        try:
            await self._set(member, entry=entry)

            if not scheduler.running:
                scheduler.start()

            scheduler.add_job(
                # Need to use partial here as it keeps sending the same user
                partial(nyame_end, member),
                id=str(entry['id']),
                trigger="date",
                next_run_time=expiration,
                replace_existing=True,
            )

            await ctx.send(
                f"{ctx.author.mention} nyamed {original_name}'s to {name} until <t:{int(expiration.timestamp())}:F>.\n"
            )

        except (PermissionError, Forbidden) as e:
            await self.config.member(ctx.author).next_nyame_available.set(
                datetime.now(tz=TIMEZONE).timestamp()
            )
            await ctx.reply(
                f"ERROR: Bot does nyot have permission to edit {member.display_name}'s nyicknyame. Nyour curse cooldown was refunded."
            )
            return

    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @nickname.command(aliases=["lock"], usage="<member> <name>")
    async def set(
        self, ctx: commands.GuildContext, member: discord.Member, *, name: str
    ):
        """Sets a stuck nickname for the user until unset.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (discord.Member): The target member whose nickname is changing.
            name (str): The name to set for the user.
        """
        name = name.strip("\"'")
        original_name: str = member.display_name
        entry = CreateNickQueueEntry(
            name=name,
            target_id=member.id,
            instigator_id=ctx.author.id,
            type="Locked",
        )
        try:
            await self._set(member, entry=entry)
            await ctx.send(f"Locked {original_name}'s nickname to {name}.")
        except (PermissionError, Forbidden) as e:
            await ctx.send(
                f"ERROR: Bot does not have permission to edit {member.display_name}'s nickname."
            )
        pass

    @commands.guild_only()
    @nickname.command(usage="<member> <name>")
    async def curse(
        self, ctx: commands.GuildContext, target: discord.Member, *, name: str
    ):
        """Attempts to curse a member with a given nickname.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (discord.Member): The target member to be afflicted.
            name (str): The name to set for the user.
        """
        name = name.strip("\"'")
        next_curse_available = await self.config.member(
            ctx.author
        ).next_curse_available()

        if (
            not ctx.author.guild_permissions.manage_roles
            and next_curse_available != None
            and next_curse_available > datetime.now(tz=TIMEZONE).timestamp()
        ):
            await ctx.reply(
                f"{ctx.author.mention}'s curse power is on cooldown.  Next available at <t:{int(next_curse_available)}:F>."
            )
            return

        if len(name) > DISCORD_MAX_NICK_LENGTH:
            await ctx.reply(
                f"{ctx.author.mention} attempted to curse with too long a name (must be less than {DISCORD_MAX_NICK_LENGTH} characters)."
            )
            return

        global_curse_cooldown_secs = await self.config.guild(ctx.guild).curse_cooldown()
        next_available = (
            datetime.now(tz=TIMEZONE).timestamp()
            + global_curse_cooldown_secs
        )
        await self.config.member(ctx.author).next_curse_available.set(next_available)

        curse_duration_seconds : int = await self.config.guild(ctx.guild).curse_duration()

        cooldown_msg = f"{ctx.author.mention}'s ability to curse is on cooldown for until <t:{int(next_available)}:F>."

        attacker_strength = await self.config.guild(ctx.guild).attacker_strength()
        defender_strength = await self.config.guild(ctx.guild).defender_strength()

        attacker_roll = d20.roll(attacker_strength)
        defender_roll = d20.roll(defender_strength)

        result_msg = (
            f"(Attacker: {attacker_roll.result}) vs. (Defender: {defender_roll.result})"
        )

        prefix = ""

        bot_role: discord.Role = ctx.guild.me.top_role
        target_role: discord.Role = target.top_role

        if (
            bot_role.position < target_role.position
            or target.guild_permissions.administrator
        ):
            await self.config.member(ctx.author).next_curse_available.set(
                datetime.now(tz=TIMEZONE).timestamp()
            )
            await ctx.reply(
                f"ERROR: Bot does not have permission to edit {target.display_name}'s nickname. Your curse cooldown was refunded."
            )
            return

        if await self.config.guild(ctx.guild).attacker_wins_ties():

            def predicate(x, y):
                return x >= y

        else:

            def predicate(x, y):
                return x > y

        cursed_users: typing.List[discord.Member] = []

        if attacker_roll.crit == d20.CritType.FAIL:
            cursed_users.append(ctx.author)
            prefix += f":skull: Oh no, something went wrong... :skull:\n"
            pass
        elif attacker_roll.crit == d20.CritType.CRIT:
            curse_duration_seconds *= 2
            prefix += f":dart: Your curse feels extra potent! :dart:\n"
            pass

        # if defender_roll.crit == d20.CritType.FAIL:
        #     pass
        if defender_roll.crit == d20.CritType.CRIT:
            prefix += f":shield: {target.display_name} shielded against the blow"
            collateral_list: typing.List[discord.Member] = []
            fetched: typing.List[discord.Message] = [
                message async for message in ctx.channel.history(limit=200)
            ]
            potentials: typing.List[discord.Member] = list(
                set([msg.author for msg in fetched])  # type: ignore[misc]
            )
            collateral_list.extend(
                [
                    t
                    for t in potentials
                    if t.id != target.id and t.id != ctx.author.id and not t.bot
                ]
            )
            collateral_list = collateral_list[0:20]

            if len(collateral_list) > 0:
                new_target = random.choice(collateral_list)
                cursed_users.append(new_target)
                prefix += f"...and it ended up hitting {new_target.display_name} ({new_target.mention}) by mistake"
                pass

            if attacker_roll.crit == d20.CritType.CRIT and predicate(
                attacker_roll.total, defender_roll.total
            ):
                prefix += f"...but {ctx.author.display_name}'s attack was too powerful"
                cursed_users.append(target)

            prefix += "!\n"
        elif predicate(attacker_roll.total, defender_roll.total):
            cursed_users.append(target)
        else:
            prefix += f"{ctx.author.mention} failed to curse {target.display_name}.\n"

        expiration : datetime = datetime.now(tz=TIMEZONE) + timedelta(seconds=curse_duration_seconds)

        if predicate(attacker_roll.total, defender_roll.total):
            prefix += f":white_check_mark: {result_msg}\n"
        else:
            prefix += f":x: {result_msg}\n"

        async def curse_end(v: discord.Member):
            try:
                await self._unset(v, type="Cursed")
                await ctx.send(
                    f"{ctx.author.display_name}'s Curse on {v.display_name} has ended."
                )
            except (PermissionError, Forbidden) as e:
                await self.config.member(ctx.author).next_curse_available.set(
                    datetime.now(tz=TIMEZONE).timestamp()
                )
                await ctx.reply(
                    f"ERROR: Bot does not have permission to edit {v.display_name}'s nickname. Please reach out to a mod uncurse your name."
                )

        for victim in cursed_users:
            original_name: str = victim.display_name

            entry = CreateNickQueueEntry(
                name=name,
                target_id=victim.id,
                instigator_id=ctx.author.id,
                type="Cursed",
                expiration=expiration,
            )

            try:
                await self._set(victim, entry=entry)

                scheduler.add_job(
                    # Need to use partial here as it keeps sending the same user
                    partial(curse_end, victim),
                    id=str(entry['id']),
                    trigger="date",
                    next_run_time=expiration,
                    replace_existing=True,
                )

                jobs = scheduler.get_jobs()

                prefix += f"{ctx.author.mention} cursed {original_name}'s nickname to {name} until <t:{int(expiration.timestamp())}:F>.\n"

            except (PermissionError, Forbidden) as e:
                if target.id == victim.id:
                    await self.config.member(ctx.author).next_curse_available.set(
                        datetime.now(tz=TIMEZONE).timestamp()
                    )
                    await ctx.reply(
                        f"ERROR: Bot does not have permission to edit {victim.display_name}'s nickname. {ctx.author.mention}'s curse cooldown was refunded."
                    )
                    return
                else:
                    continue

        await ctx.send(f"{prefix}{cooldown_msg}")
        pass

    async def _unset(self, member: discord.Member, *, id: typing.Optional[int] = None, type: typing.Optional[CurseType] = None) -> NickQueueEntry:
        """Removes a stuck nickname for a user.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (discord.Member): The target member whose nickname is changing.
        """
        if id is None and type is None:
            raise commands.BadArgument("Need to have a valid type or id to remove a job.")
        
        guild = member.guild

        member_config = bind_member(self.config.member(member))

        await member_config.remove(id=id, type=type)

        latest = await member_config.get_latest()

        await member.edit(
            reason=f"Removing current lock on nickname.", nick=latest["name"]
        )

        # if not await member_config.is_cursed() and not await member_config.is_locked():
        #     member_ids = await self.config.guild(guild).nicknamed_member_ids()
        #     member_ids = list(filter(lambda x: x != member.id, member_ids))
        #     await self.config.guild(guild).nicknamed_member_ids.set(member_ids)

        return latest

    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @nickname.command(aliases=["unlock"], usage="<member>")
    async def unset(self, ctx: commands.GuildContext, *, member: discord.Member):
        """Removes a locked nickname for a user.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (discord.Member): The target member whose nickname is changing.
        """
        member_config = bind_member(self.config.member(member))

        if not await member_config.is_locked():
            await ctx.send(f"{member.display_name} isn't locked.")
            return

        original_name = member.display_name
        try:
            latest = await self._unset(member, type="Locked")
            msg = f"Removed the lock on {original_name}, returning their nickname to {member.display_name}"
            if latest != None:
                msg += f" ({latest['type']})"
            await ctx.send(f"{msg}.")
        except (PermissionError, Forbidden) as e:
            await ctx.reply(
                f"ERROR: Bot does not have permission to edit {member.display_name}'s nickname. Your curse cooldown was refunded."
            )
        pass

    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @nickname.command(usage="<member>")
    async def unnyame(self, ctx: commands.GuildContext, *, member: discord.Member):
        """Removes a cursed nickname for a user.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (discord.Member): The target member whose nickname is changing.
        """
        member_config = bind_member(self.config.member(member))
        author: discord.Member = ctx.author

        if not await member_config.is_nyamed():
            await ctx.send(f"{member.display_name} isn't nyamed.")
            return

        if (
            author.id != await member_config.get_nyaming_instigator_id()
            and not author.guild_permissions.manage_roles
        ):
            await ctx.send(
                f"You do not have permission to remove {member.display_name}'s nyame."
            )
            return

        original_name = member.display_name

        try:
            latest = await self._unset(member, type="Nyamed")
            msg = f"Removed the nyame on {original_name}, returning their nyickname to {member.display_name}"
            if latest != None:
                msg += f" ({latest['type']})"
            await ctx.send(f"{msg}.")
        except (PermissionError, Forbidden) as e:
            await ctx.reply(
                f"ERROR: Bot does nyot have permission to edit {member.display_name}'s nyickname."
            )
        pass

    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @nickname.command(usage="<member>")
    async def uncurse(self, ctx: commands.GuildContext, *, member: discord.Member):
        """Removes a cursed nickname for a user.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (discord.Member): The target member whose nickname is changing.
        """
        member_config = bind_member(self.config.member(member))
        author: discord.Member = ctx.author

        if not await member_config.is_cursed():
            await ctx.send(f"{member.display_name} isn't cursed.")
            return

        if (
            author.id != await member_config.get_cursing_instigator_id()
            and not author.guild_permissions.manage_roles
        ):
            await ctx.send(
                f"You do not have permission to remove {member.display_name}'s curse."
            )
            return

        original_name = member.display_name

        try:
            latest = await self._unset(member, type="Cursed")
            msg = f"Removed the curse on {original_name}, returning their nickname to {member.display_name}"
            if latest != None:
                msg += f" ({latest['type']})"
            await ctx.send(f"{msg}.")
        except (PermissionError, Forbidden) as e:
            await ctx.reply(
                f"ERROR: Bot does not have permission to edit {member.display_name}'s nickname. Your curse cooldown was refunded."
            )
        pass

    async def check(
        self, ctx: commands.GuildContext, member: discord.Member, type: CurseType
    ):
        """Checks the remaining duration on a curse or the time since a name has been locked for a user.

        Args:
            ctx (commands.GuildContext): Command Context.
            member (typing.Optional[discord.Member], optional): The member to check against. Defaults to None.
            type (typing.Optional[str], optional): What type of check to perform.. Defaults to None.
        """
        member_config = bind_member(self.config.member(member))
        ailment: NickQueueEntry = {
            "Cursed": await member_config.get_latest_curse(),
            "Nyamed": await member_config.get_latest_nyame(),
            "Locked": await member_config.get_latest_lock(),
        }[type]

        if ailment is None:
            return None
        else:
            instigator: typing.Union[discord.Member, discord.User, None]

            instigator = ctx.guild.get_member(ailment['instigator_id'])  # type: ignore[arg-type]
            if instigator is None:
                instigator = await self.bot.fetch_user(ailment['instigator_id'])  # type: ignore[arg-type]

            return {
                "target": member.display_name,
                "type": ailment['type'],
                "instigator": instigator.display_name or f"**NOT FOUND**",
                "participle": f"{'until' if ailment['type'] == 'Cursed' or ailment['type'] == 'Nyamed' else 'since'} ",
                "time": datetime.fromtimestamp(
                        (
                            ailment['expiration']
                            if ailment['expiration'] is not None
                            else ailment['created_at']
                        ),
                        tz=TIMEZONE,
                    ),
            }
            pass
        pass

    @commands.guild_only()
    @nickname.command(usage="<member>")
    async def checknyame(
        self, ctx: commands.GuildContext, member: typing.Optional[discord.Member]
    ):
        """Checks the remainying nyame durationya for a user.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (typing.Optional[discord.Member], optional): The member to check against. Defaults to None.

        """
        if member is None:
            member = ctx.author

        fields = await self.check(ctx, member, "Nyamed")
        if fields == None:
            await ctx.reply(
                f"{'You are' if member == ctx.author else f'{member.display_name} is'} not currently nyamed."
            )
            return
        await ctx.reply(
            f"{fields['target']} is {fields['type']} by {fields['instigator']} {fields['participle']} <t:{int(fields['time'].timestamp())}:F>."
        )

    @commands.guild_only()
    @nickname.command(usage="<member>")
    async def checkcurse(
        self, ctx: commands.GuildContext, member: typing.Optional[discord.Member]
    ):
        """Checks the remaining curse duration for a user.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (typing.Optional[discord.Member], optional): The member to check against. Defaults to None.

        """
        if member is None:
            member = ctx.author

        fields = await self.check(ctx, member, "Cursed")
        if fields == None:
            await ctx.reply(
                f"{'You are' if member == ctx.author else f'{member.display_name} is'} not currently Cursed to a nickname."
            )
            return
        await ctx.reply(
            f"{fields['target']} is {fields['type']} by {fields['instigator']} {fields['participle']} <t:{int(fields['time'].timestamp())}:F>."
        )

    @commands.guild_only()
    @nickname.command(usage="<member>")
    async def checklock(
        self, ctx: commands.GuildContext, member: typing.Optional[discord.Member]
    ):
        """Checks the remaining lock duration for a user.

        __Args__:
            ctx (commands.GuildContext): Command Context
            member (typing.Optional[discord.Member], optional): The member to check against. Defaults to None.
        """
        if member is None:
            member = ctx.author

        fields = await self.check(ctx, member, "Locked")
        if fields == None:
            await ctx.reply(
                f"{'You are' if member == ctx.author else f'{member.display_name} is'} not currently Locked to a nickname."
            )
            return
        await ctx.reply(
            f"{fields['target']} is {fields['type']} by {fields['instigator']} {fields['participle']} <t:{int(fields['time'].timestamp())}:F>."
        )

    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @nickname.command()
    async def list(self, ctx: commands.GuildContext):
        """Displays the list of all users whose nicknames are set.

        __Args__:
            ctx (commands.GuildContext): Command Context.
        """
        guild: discord.Guild = ctx.guild

        member_ids = await self._get_member_ids_by_entry(guild)

        values: typing.List[NickQueueEntry] = []

        member: discord.Member
        for id in member_ids:
            if guild.get_member(id) is not None:
                member_config = bind_member(self.config.member(guild.get_member(id)))
                nick_queue = await member_config.nick_queue()
                nick_queue = list(
                    filter(
                        lambda entry: entry["type"] != "Default"
                        and (
                            entry["expiration"] is None
                            or entry["expiration"]
                            > datetime.now(tz=TIMEZONE).timestamp()
                        ),
                        nick_queue,
                    )
                )

                values.extend(nick_queue)
            else:
                pass

        if len(values) == 0:
            await ctx.send("No members currently have locked nicknames.")
            return

        # Sort by time locked.
        values = sorted(
            values,
            key=lambda x: x['expiration'] if x['expiration'] is not None else x['created_at'],
            reverse=True,
        )

        title = f"Cursed or Locked Nicknames"

        while len(values) > 0:
            description = ""
            while len(values) > 0:
                value = values[0]
                time_field : str
                member = guild.get_member(value['target_id'])  # type: ignore[assignment]
                author = guild.get_member(value['instigator_id']) if value['instigator_id'] is not None else None 

                if value['expiration'] is not None:
                    time_field = f"<t:{int(datetime.fromtimestamp(value['expiration'], tz=TIMEZONE).timestamp())}:F>"
                else:
                    time_field = f"<t:{int(datetime.fromtimestamp(value['created_at'], tz=TIMEZONE).timestamp())}:F>"

                string = f"{member.mention} ({member.name}) was {value['type']} to `{value['name']}`{f' by {author.mention}' if author is not None else ''}: "
                string += f" {'Releases on' if value['expiration'] is not None else 'Since'} {time_field}"

                if value['type'] == "Cursed":
                    string = f":skull:{string}"
                elif value['type'] == "Nyamed":
                    string = f":cat:{string}"
                elif value['type'] == "Locked":
                    string = f":lock:{string}"

                string += "\n"

                if (len(description) + len(string) > DISCORD_EMBED_MAX_DESCRIPTION_LENGTH):
                    break

                description += string

                values.pop(0)

            if len(description) == 0:
                await ctx.send(f"Something went wrong.")
                await self.bot.send_to_owners(
                    f"""`nickname: Failed to generate nickname list.
                    -- guild: {guild.name} <{guild.id}>
                    -- nickname_list: {values}`"""
                )

            embed = discord.Embed(title=title, description=description)

            title = ""

            await ctx.send(embed=embed)
            pass
        pass

    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @nickname.command()
    async def cooldown(
        self,
        ctx: commands.GuildContext,
        cooldown_sec: typing.Optional[typing.Union[int, str]] = None,
    ):
        """Sets or displays the current curse attempt cooldown.

        Args:
            cooldown_sec (typing.Optional[int], optional): The amount (in seconds) that the curse cooldown should be set to.
        """
        guild: discord.Guild = ctx.guild

        if cooldown_sec is None:
            cooldown_sec = int(await self.config.guild(guild).curse_cooldown())
            pass
        else:
            if isinstance(cooldown_sec, str):
                try:
                    cooldown_sec = parse_duration_string(cooldown_sec)
                except commands.BadArgument:
                    await ctx.send(
                        "Unable to parse cooldown input. Please use a valid format:\n-- HH:MM:SS\n-- MM:SS\n-- integer (seconds)"
                    )
                    return

            await self.config.guild(guild).curse_cooldown.set(cooldown_sec)

            upper_bound = (datetime.now(tz=TIMEZONE) + timedelta(seconds=cooldown_sec))

            for member in guild.members:
                next_available: datetime = datetime.fromtimestamp(
                    await self.config.member(member).next_curse_available() or 0
                , tz=TIMEZONE)

                if next_available != None and next_available > upper_bound:
                    await self.config.member(member).next_curse_available.set(
                        upper_bound.timestamp()
                    )
            pass

        seconds = cooldown_sec % 60
        minutes = int(cooldown_sec / 60) % 60
        hours = int(cooldown_sec / 60 / 60)

        await ctx.send(
            f"Curse attempt cooldown currently set to {duration_string(hours, minutes, seconds)}."
        )
        pass

    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @nickname.command()
    async def duration(
        self,
        ctx: commands.GuildContext,
        duration_sec: typing.Optional[typing.Union[int, str]] = None,
    ):
        """Sets or displays the current curse duration.

        Args:
            duration_sec (typing.Optional[typing.Union[int, str]], optional): The amount (in `HH:MM:SS` or integer) that the curse duration should be set to.
        """
        if duration_sec is None:
            duration_sec = int(await self.config.guild(ctx.guild).curse_duration())
            pass
        else:
            if isinstance(duration_sec, str):
                try:
                    duration_sec = parse_duration_string(duration_sec)
                except commands.BadArgument:
                    await ctx.send(
                        "Unable to parse duration input. Please use a valid format:\n-- HH:MM:SS\n-- MM:SS\n-- integer (seconds)"
                    )
                    return

            await self.config.guild(ctx.guild).curse_duration.set(duration_sec)
            pass

        seconds = duration_sec % 60
        minutes = int(duration_sec / 60) % 60
        hours = int(duration_sec / 60 / 60)

        await ctx.send(
            f"Curse duration currently set to {duration_string(hours, minutes, seconds)}."
        )
        pass

    async def _check_member(self, member: discord.Member):
        member_config = bind_member(self.config.member(member))
        guild: discord.Guild = member.guild

        nick_queue = await member_config.nick_queue()
        nick_queue = list(
            filter(
                lambda entry: entry["type"] == "Cursed" or entry["type"] == "Nyamed",
                nick_queue,
            )
        )

        async def undo_curse():
            await self._unset(member, type="Cursed")
            await self._unset(member, type="Nyamed")
            try:
                # await member.send(f"{guild.get_member(curse['instigator_id']).display_name}'s Curse on you has ended.")
                pass
            except discord.errors.HTTPException as e:
                print(
                    f"Attempted to send a message and failed to DM (could be bot?):\n{curse}"
                )
                pass

        for curse in nick_queue:
            if curse["expiration"] < datetime.now(tz=TIMEZONE).timestamp():
                await undo_curse()
                continue
            else:
                scheduler.add_job(
                    undo_curse,
                    id=str(curse["id"]),
                    trigger="date",
                    next_run_time=datetime.fromtimestamp(
                        curse["expiration"], tz=TIMEZONE
                    ),
                    replace_existing=True,
                )
                pass

    async def _check_guild(self, guild: discord.Guild):
        member_ids: typing.List[int] = await self._get_member_ids_by_entry(guild)

        if len(member_ids) == 0:
            return
        else:
            member: discord.Member
            for member in [m for m in [guild.get_member(id) for id in member_ids] if m is not None]:
                await self._check_member(member)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._check_guild(guild)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Checks for member nickname changes and locks them if so.

        __Args__:
            before (discord.Member): Affected member state before change.
            after (discord.Member): Affected member state after change.
        """

        # Check if nickname didn't update
        if before.nick == after.nick:
            return

        member_config = bind_member(self.config.member(before))

        # Check if nickname isn't locked.
        if (
            not await member_config.is_locked()
            and not await member_config.is_cursed()
            and not await member_config.is_nyamed()
        ):
            await member_config.replace_original(after.display_name)
            return

        # Check if nickname was changed to the locked nickname.
        latest = await member_config.get_latest()
        if latest == None or after.nick == latest["name"]:
            return

        await after.guild.get_member(after.id).edit( # type: ignore[union-attr]
            reason=f"Preventing user from changing nickname.", nick=latest["name"]
        )

        pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Restores user locked nicknames if they rejoin the server.

        __Args__:
            member (discord.Member): Affected member.
        """

        member_config = bind_member(self.config.member(member))

        # Check if nickname isn't locked.
        if (
            not await member_config.is_locked()
            and not await member_config.is_cursed()
            and not await member_config.is_nyamed()
        ):
            await member_config.replace_original(member.display_name)
            return

        # Check if nickname was changed to the locked nickname.
        latest = await member_config.get_latest()
        if latest == None or member.nick == latest["name"]:
            return

        await member.guild.get_member(member.id).edit( # type: ignore[union-attr]
            reason=f"Updating user's nickname to locked nickname.", nick=latest["name"]
        )
        pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Not used.

        __Args__:
            member (discord.Member): Affected member.
        """
        pass

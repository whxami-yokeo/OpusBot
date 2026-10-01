from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from database import (
    EVENT_TIMEZONE,
    add_keyword_event_entry,
    cancel_keyword_event,
    create_keyword_event,
    finish_keyword_event,
    get_active_keyword_events,
    get_expired_keyword_events,
    get_guild_keyword_event,
    get_keyword_event_entries,
    get_keyword_event_entry_count,
    get_keyword_event_winners,
    keyword_event_datetime_from_db,
    keyword_event_local_now,
)


MAX_EVENT_WINNERS = 25


class KeywordEvents(commands.Cog):
    """
    DM keyword event Cog.

    Staff create a keyword event with a phrase, number of winners, and deadline.

    Slash-command example:
        /keywordevent keywords:"THRIVE 2 LIVE" winners:3 deadline:"1d 1h"

    Prefix-command example:
        !keywordevent "THRIVE 2 LIVE" 3 1d 1h

    Users enter by DMing the bot a message that contains the configured phrase.
    Each person receives one entry per event.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.check_expired_events.start()

    def cog_unload(self):
        """Stop the background loop when this Cog is unloaded/reloaded."""
        self.check_expired_events.cancel()

    # -------------------------------------------------------------------------
    # Text and time helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def normalize_text(text: str) -> str:
        """
        Make matching case-insensitive and normalize repeated whitespace.

        Examples:
            "THRIVE 2 LIVE"      -> "thrive 2 live"
            "Thrive    2 Live"   -> "thrive 2 live"
        """
        return " ".join(text.casefold().split())

    @staticmethod
    def parse_deadline(text: str) -> datetime:
        """
        Parse an event deadline using Dallas/Fort Worth local time.

        Supported examples:
            tomorrow
            1d
            1h
            30m
            1d 1h
            2d 3h 15m

        Meaning:
            tomorrow = 12:00 AM at the start of the next local calendar day
            1d       = 24 hours from the command's creation time
            1d 1h    = 25 hours from the command's creation time
        """
        raw = text.strip().casefold()
        now_local = keyword_event_local_now()

        if raw == "tomorrow":
            tomorrow_date = now_local.date() + timedelta(days=1)

            return datetime(
                year=tomorrow_date.year,
                month=tomorrow_date.month,
                day=tomorrow_date.day,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
                tzinfo=EVENT_TIMEZONE,
            )

        # Match duration tokens such as 1d, 4h, and 15m.
        matches = re.findall(r"(\d+)\s*([dhm])", raw)

        # Reject invalid leftover text.
        # For example, "tomorrow at 4" or "one day" should fail.
        remainder = re.sub(r"(\d+)\s*([dhm])", "", raw)

        if not matches or remainder.strip():
            raise ValueError(
                "Invalid time format. Use `1d`, `1h`, `30m`, `1d 1h`, "
                "or `tomorrow`."
            )

        days = 0
        hours = 0
        minutes = 0

        for amount_text, unit in matches:
            amount = int(amount_text)

            if unit == "d":
                days += amount
            elif unit == "h":
                hours += amount
            elif unit == "m":
                minutes += amount

        duration = timedelta(
            days=days,
            hours=hours,
            minutes=minutes,
        )

        if duration.total_seconds() <= 0:
            raise ValueError("The deadline must be in the future.")

        return now_local + duration

    @staticmethod
    def format_discord_time(dt: datetime) -> str:
        """
        Make a Discord-formatted absolute and relative timestamp.

        Discord displays this in every viewer's own Discord/device timezone,
        while the event itself is configured from Dallas/Fort Worth time.
        """
        local_dt = dt.astimezone(EVENT_TIMEZONE)

        return (
            f"<t:{int(local_dt.timestamp())}:F> "
            f"(<t:{int(local_dt.timestamp())}:R>)"
        )

    # -------------------------------------------------------------------------
    # Event commands
    # -------------------------------------------------------------------------

    @commands.hybrid_command(
        name="keywordevent",
        description="Create a keyword event entered by DMing the bot.",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def keywordevent(
        self,
        ctx: commands.Context,
        keywords: str,
        winners: int = 1,
        *,
        deadline: str,
    ) -> None:
        """
        Create a DM keyword event.

        Slash examples:
            /keywordevent keywords:"THRIVE 2 LIVE" winners:1 deadline:"tomorrow"
            /keywordevent keywords:"THRIVE 2 LIVE" winners:3 deadline:"1d 1h"

        Prefix examples:
            !keywordevent "THRIVE 2 LIVE" 1 tomorrow
            !keywordevent "THRIVE 2 LIVE" 3 1d 1h
        """
        keywords = keywords.strip()

        if not keywords:
            await ctx.send("You must provide a keyword or phrase to listen for.")
            return

        if winners < 1:
            await ctx.send("The number of winners must be at least `1`.")
            return

        if winners > MAX_EVENT_WINNERS:
            await ctx.send(
                f"The maximum number of winners for one event is "
                f"`{MAX_EVENT_WINNERS}`."
            )
            return

        try:
            ends_at = self.parse_deadline(deadline)
        except ValueError as error:
            await ctx.send(
                f"Could not create the keyword event: {error}\n\n"
                "Examples: `1d`, `1h`, `30m`, `1d 1h`, or `tomorrow`."
            )
            return

        created_at = keyword_event_local_now()
        normalized_keywords = self.normalize_text(keywords)

        event_id = await create_keyword_event(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            created_by=ctx.author.id,
            keywords=keywords,
            normalized_keywords=normalized_keywords,
            created_at=created_at,
            ends_at=ends_at,
            winner_count=winners,
        )

        embed = discord.Embed(
            title="Keyword DM event created",
            colour=discord.Colour.green(),
        )

        embed.add_field(
            name="Event ID",
            value=str(event_id),
            inline=True,
        )

        embed.add_field(
            name="Winners",
            value=str(winners),
            inline=True,
        )

        embed.add_field(
            name="Keyword/phrase",
            value=f"`{keywords}`",
            inline=False,
        )

        embed.add_field(
            name="Deadline",
            value=self.format_discord_time(ends_at),
            inline=False,
        )

        embed.add_field(
            name="How to enter",
            value=(
                f"DM me a message containing `{keywords}` before the deadline. "
                "One entry is allowed per Discord account."
            ),
            inline=False,
        )

        if winners == 1:
            footer_text = (
                "One winner will be selected automatically after the deadline."
            )
        else:
            footer_text = (
                f"{winners} distinct winners will be selected automatically "
                "after the deadline."
            )

        embed.set_footer(text=footer_text)

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="keywordeventstatus",
        description="Show the status and entry count of a keyword event.",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def keywordeventstatus(
        self,
        ctx: commands.Context,
        event_id: int,
    ) -> None:
        """
        View event details, entry count, deadline, status, and saved winners.
        """
        event = await get_guild_keyword_event(
            event_id=event_id,
            guild_id=ctx.guild.id,
        )

        if event is None:
            await ctx.send("No keyword event with that ID exists in this server.")
            return

        entry_count = await get_keyword_event_entry_count(event_id)
        ends_at = keyword_event_datetime_from_db(event["ends_at"])

        colour = (
            discord.Colour.green()
            if event["status"] == "active"
            else discord.Colour.dark_grey()
        )

        embed = discord.Embed(
            title=f"Keyword event #{event_id}",
            colour=colour,
        )

        embed.add_field(
            name="Keyword/phrase",
            value=f"`{event['keywords']}`",
            inline=False,
        )

        embed.add_field(
            name="Status",
            value=event["status"].title(),
            inline=True,
        )

        embed.add_field(
            name="Entries",
            value=str(entry_count),
            inline=True,
        )

        embed.add_field(
            name="Winners requested",
            value=str(event.get("winner_count", 1)),
            inline=True,
        )

        embed.add_field(
            name="Deadline",
            value=self.format_discord_time(ends_at),
            inline=False,
        )

        saved_winners = await get_keyword_event_winners(event_id)

        if saved_winners:
            winner_mentions = "\n".join(
                f"{winner['position']}. <@{winner['user_id']}>"
                for winner in saved_winners
            )

            embed.add_field(
                name="Winner" if len(saved_winners) == 1 else "Winners",
                value=winner_mentions,
                inline=False,
            )

        # Compatibility fallback for events completed before multi-winner
        # support was deployed.
        elif event["winner_id"] is not None:
            embed.add_field(
                name="Winner",
                value=f"<@{event['winner_id']}>",
                inline=False,
            )

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="keywordevententries",
        description="List the entered users for a keyword event.",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def keywordevententries(
        self,
        ctx: commands.Context,
        event_id: int,
    ) -> None:
        """
        List all entrants for an event.

        This is restricted to Manage Server members because it reveals which
        users sent a qualifying DM to the bot.
        """
        event = await get_guild_keyword_event(
            event_id=event_id,
            guild_id=ctx.guild.id,
        )

        if event is None:
            await ctx.send("No keyword event with that ID exists in this server.")
            return

        entries = await get_keyword_event_entries(event_id)

        if not entries:
            await ctx.send(f"Keyword event #{event_id} currently has no entries.")
            return

        lines = [
            f"{number}. <@{entry['user_id']}> (`{entry['username']}`)"
            for number, entry in enumerate(entries, start=1)
        ]

        response = "\n".join(lines)

        # Discord's normal message content limit is 2,000 characters.
        if len(response) > 1900:
            response = response[:1900] + "\n…"

        await ctx.send(
            f"**Entries for keyword event #{event_id}:**\n{response}"
        )

    @commands.hybrid_command(
        name="keywordeventcancel",
        description="Cancel an active keyword event.",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def keywordeventcancel(
        self,
        ctx: commands.Context,
        event_id: int,
    ) -> None:
        """
        Cancel one active event from this server.

        This does not delete old entries; it just prevents future entry and
        prevents the automatic winner-selection task from choosing winners.
        """
        was_cancelled = await cancel_keyword_event(
            event_id=event_id,
            guild_id=ctx.guild.id,
        )

        if not was_cancelled:
            await ctx.send(
                "That event could not be cancelled. It may not exist in this "
                "server, may already be finished, or may already be cancelled."
            )
            return

        await ctx.send(f"Keyword event #{event_id} has been cancelled.")

    # -------------------------------------------------------------------------
    # DM keyword listener
    # -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Check incoming DMs for matching active keyword event phrases.

        A Discord DM has message.guild == None. Server messages are ignored.
        One person can get one entry in each event, enforced by the database's
        UNIQUE(event_id, user_id) constraint.
        """
        if message.author.bot:
            return

        # Ignore every message sent in a server, thread, forum post, etc.
        # Direct messages have no guild.
        if message.guild is not None:
            return

        # Ignore DMs containing no text, such as attachment-only messages.
        if not message.content:
            return

        normalized_message = self.normalize_text(message.content)
        now = keyword_event_local_now()
        active_events = await get_active_keyword_events()

        entered_event_ids: list[int] = []

        for event in active_events:
            ends_at = keyword_event_datetime_from_db(event["ends_at"])

            # Do not accept DMs at or after the deadline.
            if ends_at <= now:
                continue

            # Phrase matching is case-insensitive and whitespace-normalized.
            if event["normalized_keywords"] not in normalized_message:
                continue

            was_entered = await add_keyword_event_entry(
                event_id=event["id"],
                user_id=message.author.id,
                username=str(message.author),
                message_id=message.id,
                message_content=message.content,
                entered_at=now,
            )

            if was_entered:
                entered_event_ids.append(event["id"])

        # Acknowledge only newly created entries, never duplicate messages.
        if entered_event_ids:
            event_list = ", ".join(
                f"#{event_id}"
                for event_id in entered_event_ids
            )

            try:
                embed = discord.Embed(
                    title=f"Keyword event {event_list}",
                    description=(
                        "You have successfully been entered into the event! "
                        "A winner will be selected at random after the "
                        "deadline is over. Good luck!"
                    ),
                    color=discord.Color.magenta(),
                    timestamp=datetime.now(timezone.utc),
                ).set_footer(
                    text="Only Funds - Opus @ 2026",
                    icon_url=(
                        self.bot.user.display_avatar.url
                        if self.bot.user
                        else None
                    ),
                )

                await message.author.send(embed=embed)

            except discord.Forbidden:
                # The person may have DMs disabled/blocked. Their database
                # entry is still valid even if the acknowledgment cannot send.
                pass

    # -------------------------------------------------------------------------
    # Expiration and random-winner task
    # -------------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def check_expired_events(self) -> None:
        """
        Every 30 seconds:

        1. Find active keyword events that have passed their deadline.
        2. Retrieve valid unique entries.
        3. Randomly choose the requested number of distinct winners.
        4. Mark the event finished and persist the winner list.
        5. Announce the outcome in the original command channel.
        6. DM every selected winner, when their Discord privacy settings allow it.
        """
        now = keyword_event_local_now()
        expired_events = await get_expired_keyword_events(now)

        for event in expired_events:
            entries = await get_keyword_event_entries(event["id"])

            requested_winner_count = event.get("winner_count", 1)
            actual_winner_count = min(requested_winner_count, len(entries))

            # random.sample picks without replacement, so no one person can
            # occupy more than one winning slot.
            selected_entries = (
                random.sample(entries, k=actual_winner_count)
                if actual_winner_count > 0
                else []
            )

            winner_ids = [
                entry["user_id"]
                for entry in selected_entries
            ]

            # The database update includes status = 'active', so only one task
            # execution can save/announce the draw result.
            was_finished = await finish_keyword_event(
                event_id=event["id"],
                winner_ids=winner_ids,
                finished_at=now,
            )

            if not was_finished:
                continue

            channel = self.bot.get_channel(event["channel_id"])

            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(event["channel_id"])
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    # The event remains finished even if its old channel was
                    # deleted or the bot lacks access to it.
                    continue

            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue

            if not winner_ids:
                await channel.send(
                    f"Keyword event #{event['id']} has ended.\n"
                    f"Keyword: `{event['keywords']}`\n"
                    "No valid qualifying DM entries were received, so no "
                    "winner was selected."
                )
                continue

            winner_mentions = "\n".join(
                f"{position}. <@{user_id}>"
                for position, user_id in enumerate(winner_ids, start=1)
            )

            winner_label = "Winner" if len(winner_ids) == 1 else "Winners"

            announcement_message = (
                f"🎉 Keyword event #{event['id']} has ended!\n"
                f"Keyword: `{event['keywords']}`\n"
                f"Valid entries: **{len(entries)}**\n"
                f"{winner_label}:\n"
                f"{winner_mentions}"
            )

            # Gracefully handle an event where staff requested more winners
            # than the number of unique people who entered.
            if len(winner_ids) < requested_winner_count:
                entry_label = (
                    "entry was"
                    if len(winner_ids) == 1
                    else "entries were"
                )

                announcement_message += (
                    f"\n\nOnly **{len(winner_ids)}** unique valid "
                    f"{entry_label} available, so fewer than the requested "
                    f"**{requested_winner_count}** winners could be selected."
                )

            # Public result in the original event channel.
            await channel.send(announcement_message)

            # Private result sent individually to every winner.
            # A failure for one user does not stop DMs to the remaining winners.
            for position, user_id in enumerate(winner_ids, start=1):
                try:
                    winner_user = self.bot.get_user(user_id)

                    if winner_user is None:
                        winner_user = await self.bot.fetch_user(user_id)

                    winner_embed = discord.Embed(
                        title="🎉 You won!",
                        description=(
                            f"Congratulations! You were selected as "
                            f"winner #{position} for keyword event "
                            f"#{event['id']}."
                        ),
                        colour=discord.Colour.gold(),
                        timestamp=datetime.now(timezone.utc),
                    )

                    winner_embed.add_field(
                        name="Keyword/phrase",
                        value=f"`{event['keywords']}`",
                        inline=False,
                    )

                    winner_embed.add_field(
                        name="Your winning position",
                        value=f"#{position}",
                        inline=True,
                    )

                    winner_embed.add_field(
                        name="Total winners selected",
                        value=str(len(winner_ids)),
                        inline=True,
                    )

                    winner_embed.set_footer(
                        text="Please follow up with the server staff to claim your prize.",
                        icon_url=(
                            self.bot.user.display_avatar.url
                            if self.bot.user
                            else None
                        ),
                    )

                    await winner_user.send(embed=winner_embed)

                except (
                    discord.Forbidden,
                    discord.NotFound,
                    discord.HTTPException,
                ):
                    # DMs can fail if the winner has DMs disabled, blocked the
                    # bot, left all shared servers, or Discord has an API error.
                    # The public winner post remains the authoritative result.
                    pass

    @check_expired_events.before_loop
    async def before_check_expired_events(self) -> None:
        """Do not query Discord or the database until the bot is ready."""
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KeywordEvents(bot))
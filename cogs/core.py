# cogs/core.py
import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from database import DB_PATH, has_dm_sequence, start_dm_sequence, log_task_run
from cogs.events import VERIFIED_ROLE_ID


def format_uptime(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{sec}s")
    return " ".join(parts)


def is_admin(member: discord.Member) -> bool:
    """Check if member has Administrator permission (covers owner via role or explicit grant)."""
    return member.guild_permissions.administrator


class Core(commands.Cog, name="Core"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────────────────────────────────────
    # UPTIME
    # ───────────────────────────────────────
    @commands.command(name="uptime", help="Show bot uptime")
    async def uptime_prefix(self, ctx: commands.Context):
        await self._uptime_action(ctx, is_prefix=True)

    @app_commands.command(name="uptime", description="Show bot uptime")
    async def uptime_slash(self, interaction: discord.Interaction):
        await self._uptime_action(interaction, is_prefix=False)

    async def _uptime_action(self, target, is_prefix: bool):
        uptime_sec = self.bot.uptime
        embed = discord.Embed(
            title="⏱️ Bot Uptime",
            description=f"**{format_uptime(uptime_sec)}**",
            color=discord.Color.green()
        )
        embed.add_field(name="Seconds", value=f"`{uptime_sec:.1f}`", inline=True)
        embed.add_field(name="Latency", value=f"`{round(self.bot.latency * 1000)}ms`", inline=True)
        if is_prefix:
            await target.send(embed=embed)
        else:
            await target.response.send_message(embed=embed, ephemeral=True)

    # ───────────────────────────────────────
    # STATS
    # ───────────────────────────────────────
    @commands.command(name="stats", help="View your message stats")
    async def stats_prefix(self, ctx: commands.Context):
        await self._stats_action(ctx, is_prefix=True)

    @app_commands.command(name="stats", description="View your message stats")
    async def stats_slash(self, interaction: discord.Interaction):
        await self._stats_action(interaction, is_prefix=False)

    @staticmethod
    async def _stats_action(target, is_prefix: bool):
        user = target.author if is_prefix else target.user
        user_id = str(user.id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT message_count, last_seen FROM users WHERE user_id = ?",
                    (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            embed = discord.Embed(title="📊 Your Stats", color=discord.Color.green())
            embed.add_field(name="Messages", value=str(row["message_count"]), inline=True)
            embed.add_field(name="Last Seen", value=row["last_seen"], inline=True)
        else:
            embed = discord.Embed(
                title="❓ No Data",
                description="You haven’t sent any messages yet — say something!",
                color=discord.Color.orange()
            )
        if is_prefix:
            await target.send(embed=embed)
        else:
            await target.response.send_message(embed=embed, ephemeral=True)

    # ───────────────────────────────────────
    # INFO
    # ───────────────────────────────────────
    @commands.command(name="info", help="Bot info + DB status")
    async def info_prefix(self, ctx: commands.Context):
        await self._info_action(ctx, is_prefix=True)

    @app_commands.command(name="info", description="Bot info + DB status")
    async def info_slash(self, interaction: discord.Interaction):
        await self._info_action(interaction, is_prefix=False)

    async def _info_action(self, target, is_prefix: bool):
        uptime_sec = self.bot.uptime

        # Total members across all servers the bot can access.
        # A user in multiple servers will be counted once per server.
        available_users = sum(guild.member_count or 0 for guild in self.bot.guilds)

        embed = discord.Embed(title="🤖 Bot Info", color=discord.Color.blue())
        embed.add_field(name="Name", value=self.bot.user.name, inline=True)
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Users", value=str(available_users), inline=True)
        embed.add_field(name="Uptime", value=format_uptime(uptime_sec), inline=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("SELECT 1")
            db_status = "✅ Healthy"
        except Exception as e:
            db_status = f"❌ {e}"

        embed.add_field(name="Database", value=db_status, inline=False)

        if is_prefix:
            await target.send(embed=embed)
        else:
            await target.response.send_message(embed=embed, ephemeral=True)

    # ───────────────────────────────────────
    # ADMIN: RELOAD (Admin-Only)
    # ───────────────────────────────────────
    @commands.command(name="reload", help="Reload a cog (admin only)")
    async def reload_prefix(self, ctx: commands.Context, cog_name: str):
        await self._reload_action(ctx, cog_name, is_prefix=True)

    @app_commands.command(name="reload", description="Reload a cog (admin only)")
    @app_commands.describe(cog_name="Name of the cog to reload (e.g., core, fun)")
    async def reload_slash(self, interaction: discord.Interaction, cog_name: str):
        await self._reload_action(interaction, cog_name, is_prefix=False)

    async def _reload_action(self, target, cog_name: str, is_prefix: bool):
        member = target.author if is_prefix else target.user
        if not is_admin(member):
            msg = "⛔ This command is **admin-only**."
            if is_prefix:
                await target.send(msg, delete_after=10)
            else:
                await target.response.send_message(msg, ephemeral=True, delete_after=10)
            return

        try:
            await self.bot.reload_extension(f"cogs.{cog_name}")
            desc = f"✅ Reloaded cog: `{cog_name}`"
            color = discord.Color.green()
        except Exception as e:
            desc = f"❌ Failed to reload `{cog_name}`: `{e}`"
            color = discord.Color.red()

        embed = discord.Embed(title="🔄 Reloaded", description=desc, color=color)
        if is_prefix:
            await target.send(embed=embed)
        else:
            await target.response.send_message(embed=embed, ephemeral=False)

    # ───────────────────────────────────────
    # CUSTOM HELP COMMAND (Auto-Generated, Clean)
    # ───────────────────────────────────────
    @commands.command(name="help", help="Show this help message")
    async def help_prefix(self, ctx: commands.Context, *, command: str | None = None):
        await self._help_action(ctx, command, is_prefix=True)

    async def _help_action(self, target, cmd_name: str | None, is_prefix: bool):
        prefix = target.clean_prefix if hasattr(target, "clean_prefix") else "/"
        bot = self.bot

        # Specific command help
        if cmd_name:
            cmd = bot.get_command(cmd_name)
            if not cmd:
                msg = f"❓ Command `{cmd_name}` not found."
                if is_prefix:
                    await target.send(msg)
                else:
                    await target.response.send_message(msg, ephemeral=True)
                return
            embed = discord.Embed(
                title=f"`{prefix}{cmd.qualified_name} {cmd.signature}`",
                description=cmd.help or "No description available.",
                color=discord.Color.random()
            )
            if cmd.aliases:
                embed.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd.aliases), inline=False)
            # Show usage examples if in docstring (optional enhancement)
            if is_prefix:
                await target.send(embed=embed)
            else:
                await target.response.send_message(embed=embed, ephemeral=True)
            return

        # Bot-wide help: group by cog
        embed = discord.Embed(
            title="📘 Help",
            description=f"Use `{prefix}help <command>` for details.\n"
                        f"🔒 Admin commands require **Administrator** permission.",
            color=discord.Color.blurple()
        )

        # Fallback: group manually if get_cog_commands not available (discord.py <2.5)
        mapping = await bot.get_cog_commands()

        for cog_name, cmds in sorted(mapping.items()):
            filtered = [c for c in cmds if await c.can_run(target) if not c.hidden]
            if not filtered:
                continue
            lines = []
            for cmd in sorted(filtered, key=lambda c: c.name):
                sig = f"`{prefix}{cmd.qualified_name} {cmd.signature}`"
                help_text = (cmd.help or "No description").splitlines()[0][:50]
                lines.append(f"{sig} — {help_text}")
            embed.add_field(name=f"**{cog_name}**", value="\n".join(lines), inline=False)

        # Footer
        embed.set_footer(text=f"Requested by {target.author}", icon_url=target.author.display_avatar.url)

        if is_prefix:
            await target.send(embed=embed)
        else:
            await target.response.send_message(embed=embed, ephemeral=True)

    # ───────────────────────────────────────
    # ADMIN: SEND WELCOME DM (Admin-Only)
    # ───────────────────────────────────────
    @commands.command(name="sendwelcomedm", help="Send welcome DMs to users from audit logs (admin only)")
    async def send_welcome_dm_prefix(self, ctx: commands.Context):
        await self._send_welcome_dm_action(ctx, is_prefix=True)

    @app_commands.command(name="send_welcome_dm", description="Send welcome DMs to users from audit logs (admin only)")
    async def send_welcome_dm_slash(self, interaction: discord.Interaction):
        await self._send_welcome_dm_action(interaction, is_prefix=False)

    async def _send_welcome_dm_action(self, target, is_prefix: bool):
        user = target.author if is_prefix else target.user
        guild = target.guild

        if not is_admin(user):
            msg = "⛔ This command is **admin-only**."
            if is_prefix:
                await target.send(msg, delete_after=10)
            else:
                await target.response.send_message(msg, ephemeral=True, delete_after=10)
            return

        # Initial status message
        msg_status = "🔍 Scanning audit logs for recent joins..."
        if is_prefix:
            status_msg = await target.send(msg_status)
        else:
            await target.response.defer(ephemeral=True)
            status_msg = None

        try:
            import datetime
            sent_count = 0
            skipped_count = 0
            failed_count = 0
            error_messages = []

            # Fetch member join entries from audit log (last 100 entries)
            async for entry in guild.audit_logs(action=discord.AuditLogAction.member_join, limit=100):
                joined_user = entry.user

                if joined_user.bot:
                    continue

                # Check if user already in dm_sequences database
                sequence_exists = await has_dm_sequence(
                    guild_id=guild.id,
                    user_id=joined_user.id,
                )

                if sequence_exists:
                    skipped_count += 1
                    continue

                # Build welcome embed
                embed = discord.Embed(
                    title=f"🎉 Welcome to {guild.name}!",
                    description=(
                        f"Hey, {joined_user.mention}! Welcome in — really glad you're here.\n\n"
                        "I'm Isaac. I built this space for traders who are serious "
                        "about getting better, whether you're just starting out or "
                        "you've been at it a while.\n\n"
                        "One quick step before you dive in: click the free link above "
                        "to verify your account. Takes about 10 seconds and it "
                        "unlocks the full server.\n\n"
                        "And as a thank-you for verifying, I'll drop you my free "
                        "trading guide. It's the same foundation I'd want every new "
                        "trader here to start from.\n\n"
                        "See you inside.\n\n"
                        "https://whop.com/joined/big-tick-energy-premium-copy/"
                        "products/discord-access-c6/"
                    ),
                    color=discord.Color.magenta(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                    url=(
                        "https://whop.com/joined/big-tick-energy-premium-copy/"
                        "products/discord-access-c6/"
                    ),
                )

                embed.set_image(
                    url=(
                        "https://media.giphy.com/media/v1.Y2lkPWVjZjA1ZTQ3"
                        "N3NyY2NkdmM3aWJkajRxaWI4eWhqNm95MTNwdWd1MHRsbzlidzQzbi"
                        "ZlcD12MV9naWZzX3NlYXJjaCZjdD1n/dtkm9ArrjbvTMWzjuB/giphy.gif"
                    )
                )

                embed.set_footer(
                    text="Only Funds - Opus @ 2026",
                    icon_url=self.bot.user.display_avatar.url if self.bot.user else None,
                )

                try:
                    await joined_user.send(embed=embed)
                    sent_count += 1
                    await log_task_run(
                        "audit_log_welcome_dm_sent",
                        f"Sent welcome DM to {joined_user} ({joined_user.id}) in {guild.name}"
                    )
                except discord.Forbidden:
                    skipped_count += 1
                    error_messages.append(f"{joined_user.mention} — DMs disabled")
                except discord.HTTPException as error:
                    failed_count += 1
                    error_messages.append(f"{joined_user.mention} — {error}")

            # Build summary message
            summary = f"✅ Sent {sent_count} welcome DM(s) | Skipped {skipped_count} | Failed {failed_count}"
            if error_messages:
                summary += f"\n\n**Errors:**\n" + "\n".join(error_messages[:5])
                if len(error_messages) > 5:
                    summary += f"\n...and {len(error_messages) - 5} more"

            if is_prefix:
                if status_msg:
                    await status_msg.edit(content=summary)
                else:
                    await target.send(summary)
            else:
                await target.followup.send(summary, ephemeral=True, wait=True)

            await log_task_run(
                "audit_log_welcome_dm_batch",
                f"Admin {user} ran audit log welcome scan in {guild.name}: sent={sent_count}, skipped={skipped_count}, failed={failed_count}"
            )

        except discord.Forbidden:
            msg = "❌ I don't have permission to read audit logs. Ensure I have the 'View Audit Log' permission."
            if is_prefix:
                await target.send(msg)
            else:
                await target.followup.send(msg, ephemeral=True)
        except Exception as error:
            msg = f"❌ Error scanning audit logs: {error}"
            if is_prefix:
                await target.send(msg)
            else:
                await target.followup.send(msg, ephemeral=True)

    # ───────────────────────────────────────
    # ADMIN: SEND VERIFICATION REMINDER (Admin-Only)
    # ───────────────────────────────────────
    @commands.command(name="verificationreminder", help="Send verification reminder DM to unverified members (admin only)")
    async def verification_reminder_prefix(self, ctx: commands.Context, guild_id: int):
        await self._verification_reminder_action(ctx, guild_id, is_prefix=True)

    @app_commands.command(name="verification_reminder", description="Send verification reminder DM to unverified members (admin only)")
    @app_commands.describe(guild_id="Server ID to target for verification reminders")
    async def verification_reminder_slash(self, interaction: discord.Interaction, guild_id: str):
        try:
            guild_id_int = int(guild_id)
        except ValueError:
            await interaction.response.send_message("❌ Guild ID must be a valid number.", ephemeral=True)
            return
        await self._verification_reminder_action(interaction, guild_id_int, is_prefix=False)

    async def _verification_reminder_action(self, target, guild_id: int, is_prefix: bool):
        user = target.author if is_prefix else target.user

        if not is_admin(user):
            msg = "⛔ This command is **admin-only**."
            if is_prefix:
                await target.send(msg, delete_after=10)
            else:
                await target.response.send_message(msg, ephemeral=True, delete_after=10)
            return

        # Get the target guild
        target_guild = self.bot.get_guild(guild_id)
        if target_guild is None:
            msg = f"❌ Guild {guild_id} not found. Make sure I'm in that server."
            if is_prefix:
                await target.send(msg)
            else:
                await target.response.send_message(msg, ephemeral=True)
            return

        # Get the DEV guild for comparison
        dev_guild_id = int(__import__("os").getenv("DEV_GUILD_ID") or 0)
        if not dev_guild_id:
            msg = "❌ DEV_GUILD_ID not set in environment."
            if is_prefix:
                await target.send(msg)
            else:
                await target.response.send_message(msg, ephemeral=True)
            return

        msg_status = f"🔍 Scanning {target_guild.name} for unverified members..."
        if is_prefix:
            status_msg = await target.send(msg_status)
        else:
            await target.response.send_message(msg_status, ephemeral=True)
            status_msg = None

        try:
            import datetime
            sent_count = 0
            skipped_count = 0
            failed_count = 0
            error_messages = []

            # Fetch all members in target guild
            async for member in target_guild.fetch_members(limit=None):
                if member.bot:
                    continue

                # Check if user has verified role
                has_verified_role = any(role.id == VERIFIED_ROLE_ID for role in member.roles)
                if has_verified_role:
                    skipped_count += 1
                    continue

                # Check if user is in dev guild
                dev_guild = self.bot.get_guild(dev_guild_id)
                if dev_guild:
                    try:
                        dev_member = await dev_guild.fetch_member(member.id)
                        if dev_member:
                            skipped_count += 1
                            continue
                    except discord.NotFound:
                        pass

                # Build verification reminder embed
                embed = discord.Embed(
                    title="Haven't Verified Yet? Verify Now to Receive a Trading Guide!",
                    description=(
                        f"Hey, {member.mention}! You're invited to join our trading community, "
                        "and verification is quick and free.\n\n"
                        "By verifying, you'll unlock:\n"
                        "✅ Full server access\n"
                        "✅ My free trading guide\n"
                        "✅ Access to daily market insights\n\n"
                        "Ready to get started?\n\n"
                        "https://whop.com/joined/big-tick-energy-premium-copy/products/discord-access-c6/"
                    ),
                    color=discord.Color.magenta(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                    url=(
                        "https://whop.com/joined/big-tick-energy-premium-copy/"
                        "products/discord-access-c6/"
                    ),
                )

                embed.set_image(
                    url=(
                        "https://media.giphy.com/media/v1.Y2lkPWVjZjA1ZTQ3"
                        "N3NyY2NkdmM3aWJkajRxaWI4eWhqNm95MTNwdWd1MHRsbzlidzQzbi"
                        "ZlcD12MV9naWZzX3NlYXJjaCZjdD1n/dtkm9ArrjbvTMWzjuB/giphy.gif"
                    )
                )

                embed.set_footer(
                    text="Only Funds - Opus @ 2026",
                    icon_url=self.bot.user.display_avatar.url if self.bot.user else None,
                )

                try:
                    await member.send(embed=embed)
                    sent_count += 1
                    import logging
                    logging.info(f"✅ Verification reminder sent to {member} ({member.id})")
                    await log_task_run(
                        "verification_reminder_sent",
                        f"Sent verification reminder to {member} ({member.id}) in {target_guild.name}"
                    )
                except discord.Forbidden:
                    skipped_count += 1
                    import logging
                    logging.warning(f"⛔ Could not send verification reminder to {member} ({member.id}) — DMs disabled")
                    error_messages.append(f"{member.mention} — DMs disabled")
                except discord.HTTPException as error:
                    failed_count += 1
                    import logging
                    logging.error(f"❌ Failed to send verification reminder to {member} ({member.id}): {error}")
                    error_messages.append(f"{member.mention} — {error}")

            # Build summary message
            summary = f"✅ Sent {sent_count} verification reminder(s) | Skipped {skipped_count} | Failed {failed_count}"
            if error_messages:
                summary += f"\n\n**Errors:**\n" + "\n".join(error_messages[:5])
                if len(error_messages) > 5:
                    summary += f"\n...and {len(error_messages) - 5} more"

            if is_prefix:
                if status_msg:
                    await status_msg.edit(content=summary)
                else:
                    await target.send(summary)
            else:
                await target.followup.send(summary, ephemeral=True)

            await log_task_run(
                "verification_reminder_batch",
                f"Admin {user} sent verification reminders in {target_guild.name}: sent={sent_count}, skipped={skipped_count}, failed={failed_count}"
            )

        except discord.Forbidden:
            msg = "❌ I don't have permission to fetch members. Ensure I have the 'Read Members' permission."
            if is_prefix:
                await target.send(msg)
            else:
                await target.followup.send(msg, ephemeral=True)
        except Exception as error:
            msg = f"❌ Error sending verification reminders: {error}"
            if is_prefix:
                await target.send(msg)
            else:
                await target.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Core(bot))

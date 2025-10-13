import asyncio
import os
import time
from typing import Dict, Optional, Tuple
import re

import discord
from discord.ext import commands



# ---- Configuration ----
# Bot token can be hardcoded below, or read from the DISCORD_TOKEN environment variable.
# Replace the placeholder with your real token if you want it in-code.
BOT_TOKEN: str = os.environ["tokenbot"]

# Fixed voice channel name as requested (cannot be customized)
DARK_VOICE_CHANNEL_NAME = "dark-voice"
# Fixed text channel name for all bot messages
DARK_CHAT_CHANNEL_NAME = "dark-chat"

# Optional: path to a short alert sound to play 1 minute before a phase ends
# Put an audio file next to this script and set the file name here (e.g., alert.mp3)
ALERT_AUDIO_PATH = "alert.mp3"
ALERT_AUDIO_FULL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ALERT_AUDIO_PATH)


# ---- Bot Setup ----
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# Per-guild running cycle tasks
guild_id_to_task: Dict[int, asyncio.Task] = {}
# Track current phase per guild: "study" or "break" (or None)
guild_id_to_phase: Dict[int, Optional[str]] = {}
# Track number of completed study phases for the current cycle per guild
guild_id_to_study_count: Dict[int, int] = {}
# Remember which text channel to announce in for each guild
guild_id_to_announce_channel_id: Dict[int, int] = {}
# Track remaining time and timer tasks per guild
guild_id_to_remaining_time: Dict[int, int] = {}  # seconds
guild_id_to_timer_task: Dict[int, asyncio.Task] = {}  # countdown task
# Store original channel names to restore later
guild_id_to_original_channel_name: Dict[int, str] = {}
# Store target voice channel id for reliable edits (avoid name lookups)
guild_id_to_voice_channel_id: Dict[int, int] = {}
# Track the status message in dark-chat to edit countdown instead of renaming channels
guild_id_to_status_message_id: Dict[int, int] = {}
# Queue a one-time break extension (in minutes) to apply after current break ends
guild_id_to_pending_break_extension_minutes: Dict[int, int] = {}
# Debounce map to avoid spamming edits: (guild_id, member_id) -> last_edit_seconds
recent_member_edit_time: Dict[Tuple[int, int], float] = {}
# Minimum seconds between server-mute edits for the same member
PER_MEMBER_EDIT_COOLDOWN_SECONDS = 5.0


async def _get_dark_voice_channel(ctx: commands.Context) -> Optional[discord.VoiceChannel]:
    """
    Find the voice channel named DARK_VOICE_CHANNEL_NAME in the current guild.
    Prefer stored channel ID; fall back to exact or prefix name match.
    """
    if ctx.guild is None:
        return None
    # Try by stored ID first
    vc_id = guild_id_to_voice_channel_id.get(ctx.guild.id)
    if vc_id:
        ch = ctx.guild.get_channel(vc_id)
        if isinstance(ch, discord.VoiceChannel):
            return ch
    # Fallback: exact name, then prefix (handles renamed countdown)
    for channel in ctx.guild.voice_channels:
        if channel.name == DARK_VOICE_CHANNEL_NAME or channel.name.startswith(DARK_VOICE_CHANNEL_NAME):
            return channel
    return None


def _get_dark_text_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    for channel in guild.text_channels:
        if channel.name == DARK_CHAT_CHANNEL_NAME:
            return channel
    return None


async def _get_or_create_dark_text_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    channel = _get_dark_text_channel(guild)
    if channel is not None:
        return channel
    # Try to create the channel if missing
    try:
        channel = await guild.create_text_channel(DARK_CHAT_CHANNEL_NAME, reason="Create dark-chat for bot messages")
        return channel
    except Exception:
        # Fallback: try system channel if creation fails
        return guild.system_channel


async def _send_in_dark_chat(guild: Optional[discord.Guild], message: str) -> None:
    if guild is None:
        return
    channel = await _get_or_create_dark_text_channel(guild)
    if channel is not None:
        try:
            await channel.send(message)
        except Exception:
            pass


async def _update_channel_name(guild: discord.Guild, phase: str, minutes: int, seconds: int = 0, phase_number: int = 0, total_minutes: int = 0) -> None:
    """Deprecated: We no longer rename the voice channel; kept for compatibility."""
    return


async def _countdown_task(guild: discord.Guild, total_seconds: int, phase: str, phase_number: int, total_minutes: int = 0) -> None:
    """Countdown timer that updates a single status message in dark-chat instead of renaming channel."""
    try:
        remaining = total_seconds
        label = 'S' if phase.lower().startswith('s') else 'B'
        text_channel = await _get_or_create_dark_text_channel(guild)
        if text_channel is None:
            return
        # Always start a NEW status message for each phase
        content = f"[{label} #{phase_number}: {remaining // 60:02d}/{total_minutes:02d}]" if total_minutes > 0 else f"[{label} #{phase_number}: {remaining // 60:02d}]"
        status_msg = await text_channel.send(content)
        guild_id_to_status_message_id[guild.id] = status_msg.id
        # Loop and edit every minute
        while remaining > 0:
            await asyncio.sleep(60)
            remaining -= 60
            guild_id_to_remaining_time[guild.id] = max(remaining, 0)
            content = f"[{label} #{phase_number}: {max(remaining,0) // 60:02d}/{total_minutes:02d}]" if total_minutes > 0 else f"[{label} #{phase_number}: {max(remaining,0) // 60:02d}]"
            try:
                await status_msg.edit(content=content)
            except Exception:
                # If edit fails, try to recreate a new status message and continue
                try:
                    status_msg = await text_channel.send(content)
                    guild_id_to_status_message_id[guild.id] = status_msg.id
                except Exception:
                    pass
        # Final update to 0, then delete the message
        final_content = f"[{label} #{phase_number}: 00/{total_minutes:02d}]" if total_minutes > 0 else f"[{label} #{phase_number}: 00]"
        try:
            await status_msg.edit(content=final_content)
            # Wait a moment then delete the message
            await asyncio.sleep(2)
            await status_msg.delete()
        except Exception:
            # If deletion fails, try to edit the message to show it's completed
            try:
                await status_msg.edit(content="✅ Phase completed")
                await asyncio.sleep(3)
                await status_msg.delete()
            except Exception:
                pass
        finally:
            # Always clear the stored message ID
            guild_id_to_status_message_id.pop(guild.id, None)
            guild_id_to_remaining_time[guild.id] = 0
            # Best-effort cleanup of any leftover countdown messages like "[B #0: 00/02]"
            try:
                await _cleanup_countdown_messages_in_dark_chat(guild)
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def _mute_all_in_channel(channel: discord.VoiceChannel, mute: bool) -> None:
    """
    Apply server mute to ALL members currently connected in the voice channel.
    Requires the bot to have the Mute Members permission in the guild/channel.
    """
    if not channel.members:
        return
    tasks = []
    for member in channel.members:
        # Do not server-mute the bot itself, otherwise it cannot play alert audio
        if member.bot or (bot.user and member.id == bot.user.id):
            continue
        # Only attempt if the member is in a voice state in this channel
        if member.voice is None or member.voice.channel != channel:
            continue
        # Avoid redundant edits when possible
        if member.voice.mute == mute:
            continue
        tasks.append(member.edit(mute=mute, reason="Learning cycle server mute"))
    if tasks:
        # Best-effort: ignore failures for individual members
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Optional: could log exceptions if needed


async def _cycle_task(guild: discord.Guild, study_minutes: int, break_minutes: int) -> None:
    """
    Background loop per guild: enforce mute (speak=False) during study, then unmute (speak=True)
    during break, repeating until cancelled by !stop or task cancellation.
    """
    try:
        while True:
            channel = discord.utils.get(guild.voice_channels, name=DARK_VOICE_CHANNEL_NAME)
            if channel is None:
                # If channel is missing, wait a bit and retry; do not crash the task
                await asyncio.sleep(15)
                continue

            # Study phase: server mute everyone
            guild_id_to_phase[guild.id] = "study"
            await _mute_all_in_channel(channel, mute=True)
            
            # Start countdown timer for study phase
            study_phase_number = guild_id_to_study_count.get(guild.id, 0) + 1
            if guild_id_to_timer_task.get(guild.id):
                guild_id_to_timer_task[guild.id].cancel()
            guild_id_to_timer_task[guild.id] = bot.loop.create_task(
                _countdown_task(guild, study_minutes * 60, "Study", study_phase_number, study_minutes)
            )
            
            if study_minutes > 1:
                await asyncio.sleep((study_minutes - 1) * 60)
                await _one_minute_alert(guild, channel, phase_name="study")
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(study_minutes * 60)

            # Study finished → increment counter and announce
            try:
                guild_id_to_study_count[guild.id] = guild_id_to_study_count.get(guild.id, 0) + 1
                announce_channel = None
                ch_id = guild_id_to_announce_channel_id.get(guild.id)
                if ch_id:
                    announce_channel = guild.get_channel(ch_id)
                if announce_channel is None:
                    announce_channel = _get_dark_text_channel(guild)
                if announce_channel is not None:
                    count_num = guild_id_to_study_count[guild.id]
                    await _send_in_dark_chat(
                        guild,
                        f"✅ Finished {study_minutes}m. cycle: {count_num}."
                    )
            except Exception:
                pass

            # Break phase: unmute everyone
            # If break_minutes is 0, skip quickly
            if break_minutes > 0:
                guild_id_to_phase[guild.id] = "break"
                await _mute_all_in_channel(channel, mute=False)
                
                # Start countdown timer for break phase
                if guild_id_to_timer_task.get(guild.id):
                    guild_id_to_timer_task[guild.id].cancel()
                guild_id_to_timer_task[guild.id] = bot.loop.create_task(
                    _countdown_task(guild, break_minutes * 60, "Break", 0, break_minutes)
                )
                
                if break_minutes > 1:
                    await asyncio.sleep((break_minutes - 1) * 60)
                    await _one_minute_alert(guild, channel, phase_name="break")
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(break_minutes * 60)

                # After the scheduled break, apply a one-time extension if queued
                extra = guild_id_to_pending_break_extension_minutes.pop(guild.id, 0)
                if extra and extra > 0:
                    # Start an additional break segment
                    guild_id_to_phase[guild.id] = "break"
                    await _mute_all_in_channel(channel, mute=False)
                    if guild_id_to_timer_task.get(guild.id):
                        guild_id_to_timer_task[guild.id].cancel()
                    guild_id_to_timer_task[guild.id] = bot.loop.create_task(
                        _countdown_task(guild, extra * 60, "Break+", 0, extra)
                    )
                    if extra > 1:
                        await asyncio.sleep((extra - 1) * 60)
                        await _one_minute_alert(guild, channel, phase_name="break")
                        await asyncio.sleep(60)
                    else:
                        await asyncio.sleep(extra * 60)
            else:
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        # On cancellation, try to leave the channel unmuted (server unmute)
        try:
            channel = discord.utils.get(guild.voice_channels, name=DARK_VOICE_CHANNEL_NAME)
            if channel is not None:
                await _mute_all_in_channel(channel, mute=False)
            guild_id_to_phase[guild.id] = None
            # Reset study counter for this guild when the cycle stops
            guild_id_to_study_count[guild.id] = 0
        finally:
            raise


async def _one_minute_alert(guild: discord.Guild, channel: discord.VoiceChannel, phase_name: str) -> None:
    """
    Attempt to signal that 1 minute remains in the current phase by:
    - Playing a short sound in the voice channel if ALERT_AUDIO_PATH exists and FFmpeg/voice is available
    - Sending a text message in the system channel as a fallback
    """
    # Try to play a short sound in the voice channel
    try:
        file_exists = os.path.isfile(ALERT_AUDIO_FULL_PATH)
        print(f"[alert] file_exists={file_exists} path={ALERT_AUDIO_FULL_PATH}")
        if file_exists:
            voice_client = discord.utils.get(bot.voice_clients, guild=guild)
            try:
                if voice_client is None or not voice_client.is_connected():
                    print("[alert] connecting to voice...")
                    voice_client = await channel.connect(timeout=8.0, reconnect=False)
                elif voice_client.channel != channel:
                    print("[alert] moving voice client to target channel...")
                    await voice_client.move_to(channel)
            except discord.ClientException as e:
                print(f"[alert] voice connect/move error: {e}")
                return
            except discord.Forbidden as e:
                print(f"[alert] missing permission to connect/move: {e}")
                return

            # Ensure the bot member itself is not server-muted before playing
            try:
                me = guild.me
                if me is not None and me.voice is not None and me.voice.mute:
                    print("[alert] bot is server-muted; attempting to unmute self...")
                    await me.edit(mute=False, reason="Enable alert playback")
            except Exception as e:
                print(f"[alert] failed to unmute bot user: {e}")

            if voice_client is not None and not voice_client.is_playing():
                try:
                    # Wrap with volume control in case the file is quiet
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(ALERT_AUDIO_FULL_PATH), volume=1.1
                    )
                    # Wait a brief moment before starting playback
                    await asyncio.sleep(0.4)
                    voice_client.play(source)
                    print("[alert] playing alert audio...")
                    # Wait briefly (up to 1 second) so a beep can be heard
                    waited = 0.0
                    while voice_client.is_playing() and waited < 1.0:
                        await asyncio.sleep(0.1)
                        waited += 0.1
                    if not voice_client.is_playing():
                        print("[alert] playback finished or did not start.")
                    # Wait a brief moment after playback
                    await asyncio.sleep(0.4)
                except Exception as e:
                    print(f"[alert] playback error: {e}")
                finally:
                    # Disconnect after playback to free the voice connection
                    await _disconnect_voice(guild)
    except Exception as e:
        # Log and fall back
        print(f"[alert] unexpected error: {e}")

    # # Text fallback/duplicate notice
    # try:
    #     text_channel = guild.system_channel
    #     if text_channel is not None:
    #         await text_channel.send(
    #             f"⏰ One minute left in {phase_name} for #{DARK_VOICE_CHANNEL_NAME}."
    #         )
    # except Exception:
    #     pass


def _is_cycle_running(guild_id: int) -> bool:
    task = guild_id_to_task.get(guild_id)
    return task is not None and not task.done()


async def _disconnect_voice(guild: discord.Guild) -> None:
    """Disconnect the bot's voice client in this guild if connected."""
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if vc is not None and vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass


async def _purge_bot_messages_in_channel(channel: discord.TextChannel, batch_size: int = 1000, max_rounds: int = 10) -> int:
    """Delete the bot's messages in the given channel. Returns number deleted.
    Uses purge in batches; limited to recent messages Discord allows for bulk deletion.
    """
    total = 0
    try:
        for _ in range(max_rounds):
            deleted = await channel.purge(limit=batch_size, check=lambda m: m.author == bot.user)
            count = len(deleted)
            total += count
            if count == 0:
                break
    except discord.Forbidden:
        pass
    except Exception:
        pass
    return total


async def _cleanup_countdown_messages_in_dark_chat(guild: discord.Guild, limit: int = 500) -> int:
    """Delete messages in dark-chat that look like countdown statuses, e.g. "[B #0: 00/02]".
    Returns number deleted.
    """
    text_channel = await _get_or_create_dark_text_channel(guild)
    if text_channel is None:
        return 0
    countdown_pattern = re.compile(r"^\[[SB] #\d+: \d{2}(?:/\d{2})?\]$")
    try:
        deleted = await text_channel.purge(
            limit=limit,
            check=lambda m: (m.author == bot.user) and isinstance(m.content, str) and (countdown_pattern.match(m.content) is not None),
        )
        return len(deleted)
    except Exception:
        return 0


@bot.command(name="clear")
@commands.guild_only()
async def clear_bot_messages(ctx: commands.Context):
    """Delete previous messages sent by this bot across all text channels in the server."""
    if ctx.guild is None:
        return
    deleted_total = 0
    for ch in ctx.guild.text_channels:
        deleted_total += await _purge_bot_messages_in_channel(ch)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


@bot.command(name="learn")
@commands.guild_only()
async def learn(ctx: commands.Context, study_minutes: int, break_minutes: int):
    """
    Start the study/break cycle in the fixed voice channel "dark-voice".
    Usage: !learn STUDY_DURATION BREAK_DURATION (minutes)
    Example: !learn 50 10
    """
    if study_minutes < 1 or study_minutes > 24 * 60:
        await _send_in_dark_chat(ctx.guild, "Please provide STUDY_DURATION between 1 and 1440 minutes.")
        return
    if break_minutes < 0 or break_minutes > 24 * 60:
        await _send_in_dark_chat(ctx.guild, "Please provide BREAK_DURATION between 0 and 1440 minutes.")
        return

    if ctx.guild is None:
        await _send_in_dark_chat(None, "This command can only be used in a server.")
        return

    if _is_cycle_running(ctx.guild.id):
        await _send_in_dark_chat(ctx.guild, "A learning cycle is already running in this server. Use !stop to end it.")
        return

    channel = await _get_dark_voice_channel(ctx)
    if channel is None:
        await _send_in_dark_chat(ctx.guild, f"Voice channel '{DARK_VOICE_CHANNEL_NAME}' was not found. Please create it.")
        return

    # Immediate server mute to start
    try:
        await _send_in_dark_chat(
        ctx.guild,
        f"▶️ Start study {study_minutes}m / break {break_minutes}m.  !stop to end.",
    )

        await _mute_all_in_channel(channel, mute=True)
        guild_id_to_phase[ctx.guild.id] = "study"
    except discord.Forbidden:
        await _send_in_dark_chat(ctx.guild, "I need the 'Mute Members' permission to server mute in that channel.")
        return

    task = bot.loop.create_task(_cycle_task(ctx.guild, study_minutes, break_minutes))
    guild_id_to_task[ctx.guild.id] = task
    # Remember where to announce counts (the channel where the command was invoked)
    guild_id_to_announce_channel_id[ctx.guild.id] = ctx.channel.id
    # Reset study count at start of a new cycle
    guild_id_to_study_count[ctx.guild.id] = 0
    
    # Store original channel name to restore later
    try:
        channel = discord.utils.get(ctx.guild.voice_channels, name=DARK_VOICE_CHANNEL_NAME)
        if channel:
            guild_id_to_original_channel_name[ctx.guild.id] = channel.name
            guild_id_to_voice_channel_id[ctx.guild.id] = channel.id
    except Exception:
        pass
    # Clear any previous status message pointer
    guild_id_to_status_message_id.pop(ctx.guild.id, None)
    # Clear any leftover queued extension
    guild_id_to_pending_break_extension_minutes.pop(ctx.guild.id, None)


@bot.command(name="stop")
@commands.guild_only()
async def stop_cycle(ctx: commands.Context):
    """Stop the running learning cycle and reset channel permissions."""
    if ctx.guild is None:
        await _send_in_dark_chat(None, "This command can only be used in a server.")
        return

    task = guild_id_to_task.get(ctx.guild.id)
    if task is None or task.done():
        await _send_in_dark_chat(ctx.guild, "ℹ️ No cycle running.")
        return

    # Clear phase first to avoid any event-based remute during stop
    guild_id_to_phase[ctx.guild.id] = None

    # Capture completed study count before the task resets it
    completed_count = guild_id_to_study_count.get(ctx.guild.id, 0)

    # Cancel countdown timer
    timer_task = guild_id_to_timer_task.get(ctx.guild.id)
    if timer_task:
        timer_task.cancel()
        guild_id_to_timer_task.pop(ctx.guild.id, None)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        guild_id_to_task.pop(ctx.guild.id, None)

    # Ensure channel is left unmuted (server unmute)
    channel = await _get_dark_voice_channel(ctx)
    if channel is not None:
        try:
            await _mute_all_in_channel(channel, mute=False)
            # Safety: after a tiny delay, unmute again in case of race with prior loop step
            await asyncio.sleep(0.5)
            await _mute_all_in_channel(channel, mute=False)
        except discord.Forbidden:
            # If we cannot reset, at least inform the user
            await _send_in_dark_chat(ctx.guild, "Stopped. I could not reset channel permissions; please check Manage Channels permission.")
            return

    # Restore original channel name
    try:
        channel: Optional[discord.VoiceChannel] = None
        vc_id = guild_id_to_voice_channel_id.get(ctx.guild.id)
        if vc_id:
            ch = ctx.guild.get_channel(vc_id)
            if isinstance(ch, discord.VoiceChannel):
                channel = ch
        if channel is None:
            for ch in ctx.guild.voice_channels:
                if ch.name.startswith(DARK_VOICE_CHANNEL_NAME):
                    channel = ch
                    break
        if channel:
            original_name = guild_id_to_original_channel_name.get(ctx.guild.id, DARK_VOICE_CHANNEL_NAME)
            # Keep voice channel name constant per user request; set to base name
            await channel.edit(name=DARK_VOICE_CHANNEL_NAME)
    except Exception:
        pass

    # Disconnect from voice if connected
    await _disconnect_voice(ctx.guild)

    # Clean up any remaining status messages
    try:
        status_msg_id = guild_id_to_status_message_id.get(ctx.guild.id)
        if status_msg_id:
            text_channel = _get_dark_text_channel(ctx.guild)
            if text_channel:
                try:
                    status_msg = await text_channel.fetch_message(status_msg_id)
                    await status_msg.delete()
                except Exception:
                    pass
    except Exception:
        pass

    # Best-effort cleanup of any leftover countdown messages like "[B #0: 00/02]"
    try:
        await _cleanup_countdown_messages_in_dark_chat(ctx.guild)
    except Exception:
        pass

    # Clear any queued break extension
    guild_id_to_pending_break_extension_minutes.pop(ctx.guild.id, None)

    # Send stop confirmation and summary
    await _send_in_dark_chat(ctx.guild, f"📘 study finished: {completed_count} cycles.")
    # Clear status message pointer for next cycle/phase
    guild_id_to_status_message_id.pop(ctx.guild.id, None)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """
    When a user joins/leaves/moves, ensure their mute state matches current phase if a cycle is running.
    """
    # Ignore bot state changes (including this bot), so we don't mute ourselves when joining to play the alert
    if member.bot:
        return
    guild = member.guild
    phase = guild_id_to_phase.get(guild.id)
    if not phase:
        return

    # Only act if the target channel is dark-voice
    target_channel: Optional[discord.VoiceChannel] = None
    vc_id = guild_id_to_voice_channel_id.get(guild.id)
    if vc_id:
        ch = guild.get_channel(vc_id)
        if isinstance(ch, discord.VoiceChannel):
            target_channel = ch
    if target_channel is None:
        # Fallback: name startswith to cope with renamed channel (countdown suffix)
        for ch in guild.voice_channels:
            if ch.name == DARK_VOICE_CHANNEL_NAME or ch.name.startswith(DARK_VOICE_CHANNEL_NAME):
                target_channel = ch
                break
    if target_channel is None:
        return

    joined_channel = after.channel
    left_channel = before.channel

    try:
        if joined_channel and target_channel and joined_channel.id == target_channel.id:
            # Enforce current phase - always apply, ignore cooldown for joins
            desired_mute = phase == "study"
            current_mute = after.mute
            if current_mute != desired_mute:
                now = time.time()
                key = (guild.id, member.id)
                recent_member_edit_time[key] = now
                await member.edit(mute=desired_mute, reason="Learning cycle server mute (join/update)")
        elif left_channel and target_channel and left_channel.id == target_channel.id and (not joined_channel or joined_channel.id != target_channel.id):
            # Member left the channel: best-effort unmute if still muted
            if member.voice is not None and member.voice.mute:
                now = time.time()
                key = (guild.id, member.id)
                last = recent_member_edit_time.get(key, 0.0)
                if now - last >= PER_MEMBER_EDIT_COOLDOWN_SECONDS:
                    recent_member_edit_time[key] = now
                    await member.edit(mute=False, reason="Learning cycle cleanup (left channel)")
    except discord.Forbidden:
        pass


@bot.command(name="unmute")
@commands.guild_only()
async def unmute_command(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Server-unmute a mentioned member, or everyone in the server if no mention."""
    if ctx.guild is None:
        return
    try:
        if member is None:
            # No mention -> unmute everyone in the server
            unmuted_count = 0
            for voice_channel in ctx.guild.voice_channels:
                for member_in_channel in voice_channel.members:
                    if member_in_channel.voice and member_in_channel.voice.mute:
                        try:
                            await member_in_channel.edit(mute=False, reason=f"Server-wide unmute by {ctx.author}")
                            unmuted_count += 1
                        except Exception:
                            pass
            return
        # Unmute specific user
        await member.edit(mute=False, reason=f"Manual unmute by {ctx.author}")
    except discord.Forbidden:
        await _send_in_dark_chat(ctx.guild, "🔒 Can't unmute: missing permission or role below target.")
    except Exception as e:
        await _send_in_dark_chat(ctx.guild, f"⚠️ Unmute failed: {e}")


@bot.command(name="clearcommands")
@commands.guild_only()
async def clear_bot_commands(ctx: commands.Context):
    """Delete messages that are commands to this bot (starting with supported ! commands)."""
    if ctx.guild is None:
        return
    prefixes = ("!")
    known = {"learn", "stop", "unmute", "clear", "clearcommands"}
    def is_command_msg(m: discord.Message) -> bool:
        if not m.content:
            return False
        s = m.content.lstrip()
        if not s.startswith("!"):
            return False
        # Extract command keyword after '!'
        try:
            cmd = s[1:].split()[0].lower()
        except Exception:
            return False
        return cmd in known
    deleted_total = 0
    for ch in ctx.guild.text_channels:
        try:
            deleted = await ch.purge(limit=1000, check=is_command_msg)
            deleted_total += len(deleted)
        except Exception:
            pass


@bot.command(name="extendbreak")
@commands.guild_only()
async def extend_break_once(ctx: commands.Context, extra_minutes: int):
    """Queue a one-time extension to the current/next break while the cycle is running.
    Usage: !extendbreak 5
    """
    if ctx.guild is None:
        return
    if extra_minutes < 1 or extra_minutes > 1440:
        await _send_in_dark_chat(ctx.guild, "Please provide EXTRA minutes between 1 and 1440.")
        return
    if not _is_cycle_running(ctx.guild.id):
        await _send_in_dark_chat(ctx.guild, "No running cycle. Use !learn first.")
        return
    # Set/overwrite the pending extension; it will apply after the current scheduled break completes
    guild_id_to_pending_break_extension_minutes[ctx.guild.id] = extra_minutes
    await _send_in_dark_chat(ctx.guild, f"🕒 Will extend the break by {extra_minutes} minute(s) once.")


def _run():
    # Prefer hardcoded token if replaced; otherwise fallback to environment variable
    token = BOT_TOKEN 
    if not token:
        print(
            "No Discord token found. Either set BOT_TOKEN in the file (replace the placeholder), "
         
        )
        return
    bot.run(token)


if __name__ == "__main__":
    _run()

import asyncio
import os
import time
import re
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# ---- Configuration ----
BOT_TOKEN: str = os.environ["tokenbot"]

DARK_VOICE_CHANNEL_NAME = "dark-voice"
DARK_CHAT_CHANNEL_NAME = "dark-chat"
ALERT_AUDIO_PATH = "alert.mp3"
ALERT_AUDIO_FULL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ALERT_AUDIO_PATH)

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

# Bot Setup
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# State tracking dicts
guild_id_to_task = {}
guild_id_to_phase = {}
guild_id_to_study_count = {}
guild_id_to_announce_channel_id = {}
guild_id_to_remaining_time = {}
guild_id_to_timer_task = {}
guild_id_to_original_channel_name = {}
guild_id_to_voice_channel_id = {}
guild_id_to_status_message_id = {}
recent_member_edit_time = {}
PER_MEMBER_EDIT_COOLDOWN_SECONDS = 5.0

async def _get_dark_voice_channel(guild: discord.Guild) -> Optional[discord.VoiceChannel]:
    if guild is None:
        return None
    vc_id = guild_id_to_voice_channel_id.get(guild.id)
    if vc_id:
        ch = guild.get_channel(vc_id)
        if isinstance(ch, discord.VoiceChannel):
            return ch
    for channel in guild.voice_channels:
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
    try:
        channel = await guild.create_text_channel(DARK_CHAT_CHANNEL_NAME, reason="Create dark-chat for bot messages")
        return channel
    except Exception:
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

async def _countdown_task(guild: discord.Guild, total_seconds: int, phase: str, phase_number: int, total_minutes: int = 0) -> None:
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

        # === Additional cleanup: delete all countdown messages in the channel ===
        # Pattern to match countdown messages like "[B #0: 00/02]" or "[S #1: 00/50]"
        countdown_pattern = re.compile(r'^\[[SB] #\d+: \d{2}(?:/\d{2})?\]$')
        try:
            async for msg in text_channel.history(limit=100):
                if msg.author == bot.user and countdown_pattern.match(msg.content):
                    await msg.delete()
        except Exception:
            pass

    except asyncio.CancelledError:
        pass

async def _mute_all_in_channel(channel: discord.VoiceChannel, mute: bool) -> None:
    if not channel.members:
        return
    tasks = []
    for member in channel.members:
        if member.bot or (bot.user and member.id == bot.user.id):
            continue
        if member.voice is None or member.voice.channel != channel:
            continue
        if member.voice.mute == mute:
            continue
        tasks.append(member.edit(mute=mute, reason="Learning cycle server mute"))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def _cycle_task(guild: discord.Guild, study_minutes: int, break_minutes: int) -> None:
    try:
        while True:
            channel = discord.utils.get(guild.voice_channels, name=DARK_VOICE_CHANNEL_NAME)
            if channel is None:
                await asyncio.sleep(15)
                continue

            guild_id_to_phase[guild.id] = "study"
            await _mute_all_in_channel(channel, mute=True)

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

            if break_minutes > 0:
                guild_id_to_phase[guild.id] = "break"
                await _mute_all_in_channel(channel, mute=False)

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
            else:
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        try:
            channel = discord.utils.get(guild.voice_channels, name=DARK_VOICE_CHANNEL_NAME)
            if channel is not None:
                await _mute_all_in_channel(channel, mute=False)
            guild_id_to_phase[guild.id] = None
            guild_id_to_study_count[guild.id] = 0
        finally:
            raise

async def _one_minute_alert(guild: discord.Guild, channel: discord.VoiceChannel, phase_name: str) -> None:
    try:
        file_exists = os.path.isfile(ALERT_AUDIO_FULL_PATH)
        if file_exists:
            voice_client = discord.utils.get(bot.voice_clients, guild=guild)
            try:
                if voice_client is None or not voice_client.is_connected():
                    voice_client = await channel.connect(timeout=8.0, reconnect=False)
                elif voice_client.channel != channel:
                    await voice_client.move_to(channel)
            except discord.ClientException:
                return
            except discord.Forbidden:
                return

            try:
                me = guild.me
                if me is not None and me.voice is not None and me.voice.mute:
                    await me.edit(mute=False, reason="Enable alert playback")
            except Exception:
                pass

            if voice_client is not None and not voice_client.is_playing():
                try:
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(ALERT_AUDIO_FULL_PATH), volume=1.5
                    )
                    voice_client.play(source)
                    await asyncio.sleep(0.2)
                    waited = 0.0
                    while voice_client.is_playing() and waited < 1.0:
                        await asyncio.sleep(0.1)
                        waited += 0.1
                except Exception:
                    pass
                finally:
                    await _disconnect_voice(guild)
    except Exception:
        pass

def _is_cycle_running(guild_id: int) -> bool:
    task = guild_id_to_task.get(guild_id)
    return task is not None and not task.done()

async def _disconnect_voice(guild: discord.Guild) -> None:
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if vc is not None and vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

async def _purge_bot_messages_in_channel(channel: discord.TextChannel, batch_size: int = 1000, max_rounds: int = 10) -> int:
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

# Slash commands:

@tree.command(name="learn", description="Start the study/break cycle in dark-voice")
@app_commands.describe(study_minutes="Study duration in minutes (1-1440)", break_minutes="Break duration in minutes (0-1440)")
async def learn(interaction: discord.Interaction, study_minutes: int, break_minutes: int):
    if study_minutes < 1 or study_minutes > 1440:
        await interaction.response.send_message("Please provide STUDY_DURATION between 1 and 1440 minutes.", ephemeral=True)
        return
    if break_minutes < 0 or break_minutes > 1440:
        await interaction.response.send_message("Please provide BREAK_DURATION between 0 and 1440 minutes.", ephemeral=True)
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    if _is_cycle_running(guild.id):
        await interaction.response.send_message("A learning cycle is already running in this server. Use /stop to end it.", ephemeral=True)
        return
    channel = await _get_dark_voice_channel(guild)
    if channel is None:
        await interaction.response.send_message(f"Voice channel '{DARK_VOICE_CHANNEL_NAME}' was not found. Please create it.", ephemeral=True)
        return
    try:
        await _send_in_dark_chat(guild, f"▶️ Start study {study_minutes}m / break {break_minutes}m.  /stop to end.")
        await _mute_all_in_channel(channel, mute=True)
        guild_id_to_phase[guild.id] = "study"
    except discord.Forbidden:
        await interaction.response.send_message("I need the 'Mute Members' permission to server mute in that channel.", ephemeral=True)
        return
    task = bot.loop.create_task(_cycle_task(guild, study_minutes, break_minutes))
    guild_id_to_task[guild.id] = task
    guild_id_to_announce_channel_id[guild.id] = interaction.channel_id
    guild_id_to_study_count[guild.id] = 0
    try:
        guild_voice_channel = discord.utils.get(guild.voice_channels, name=DARK_VOICE_CHANNEL_NAME)
        if guild_voice_channel:
            guild_id_to_original_channel_name[guild.id] = guild_voice_channel.name
            guild_id_to_voice_channel_id[guild.id] = guild_voice_channel.id
    except Exception:
        pass
    guild_id_to_status_message_id.pop(guild.id, None)
    await interaction.response.send_message(f"Started study cycle: {study_minutes} minutes study, {break_minutes} minutes break.", ephemeral=True)

@tree.command(name="stop", description="Stop the running learning cycle")
async def stop(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    task = guild_id_to_task.get(guild.id)
    if task is None or task.done():
        await interaction.response.send_message("ℹ️ No cycle running.", ephemeral=True)
        return
    guild_id_to_phase[guild.id] = None
    completed_count = guild_id_to_study_count.get(guild.id, 0)
    timer_task = guild_id_to_timer_task.get(guild.id)
    if timer_task:
        timer_task.cancel()
        guild_id_to_timer_task.pop(guild.id, None)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        guild_id_to_task.pop(guild.id, None)
    channel = await _get_dark_voice_channel(guild)
    if channel is not None:
        try:
            await _mute_all_in_channel(channel, mute=False)
            await asyncio.sleep(0.5)
            await _mute_all_in_channel(channel, mute=False)
        except discord.Forbidden:
            await _send_in_dark_chat(guild, "Stopped. I could not reset channel permissions; please check Manage Channels permission.")
            await interaction.response.send_message("Stopped but could not reset channel permissions.", ephemeral=True)
            return
    try:
        vc_id = guild_id_to_voice_channel_id.get(guild.id)
        voice_channel = None
        if vc_id:
            ch = guild.get_channel(vc_id)
            if isinstance(ch, discord.VoiceChannel):
                voice_channel = ch
        if voice_channel is None:
            for ch in guild.voice_channels:
                if ch.name.startswith(DARK_VOICE_CHANNEL_NAME):
                    voice_channel = ch
                    break
        if voice_channel:
            await voice_channel.edit(name=DARK_VOICE_CHANNEL_NAME)
    except Exception:
        pass
    await _disconnect_voice(guild)
    try:
        status_msg_id = guild_id_to_status_message_id.get(guild.id)
        if status_msg_id:
            text_channel = _get_dark_text_channel(guild)
            if text_channel:
                try:
                    status_msg = await text_channel.fetch_message(status_msg_id)
                    await status_msg.delete()
                except Exception:
                    pass
    except Exception:
        pass
    await _send_in_dark_chat(guild, f"📘 study finished: {completed_count} cycles.")
    guild_id_to_status_message_id.pop(guild.id, None)
    await interaction.response.send_message(f"Stopped study cycle. Total cycles completed: {completed_count}.", ephemeral=True)

@tree.command(name="unmute", description="Unmute a member or all members in the server")
@app_commands.describe(member="Member to unmute (optional, all if left empty)")
async def unmute(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    try:
        if member is None:
            unmuted_count = 0
            for voice_channel in guild.voice_channels:
                for member_in_channel in voice_channel.members:
                    if member_in_channel.voice and member_in_channel.voice.mute:
                        try:
                            await member_in_channel.edit(mute=False, reason=f"Server-wide unmute by {interaction.user}")
                            unmuted_count += 1
                        except Exception:
                            pass
            await interaction.response.send_message(f"Unmuted {unmuted_count} members.", ephemeral=True)
            return
        await member.edit(mute=False, reason=f"Manual unmute by {interaction.user}")
        await interaction.response.send_message(f"Unmuted {member.display_name}.", ephemeral=True)
    except discord.Forbidden:
        await _send_in_dark_chat(guild, "🔒 Can't unmute: missing permission or role below target.")
        await interaction.response.send_message("Can't unmute due to permission.", ephemeral=True)
    except Exception as e:
        await _send_in_dark_chat(guild, f"⚠️ Unmute failed: {e}")
        await interaction.response.send_message("Unmute failed.", ephemeral=True)

@tree.command(name="clear", description="Delete previous messages sent by the bot in this server")
async def clear(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    deleted_total = 0
    for ch in guild.text_channels:
        deleted_total += await _purge_bot_messages_in_channel(ch)
    await interaction.response.send_message(f"Deleted {deleted_total} bot messages.", ephemeral=True)

@tree.command(name="clearcommands", description="Delete bot command messages starting with !")
async def clearcommands(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    def is_command_msg(m: discord.Message) -> bool:
        if not m.content:
            return False
        s = m.content.lstrip()
        if not s.startswith("!"):
            return False
        try:
            cmd = s[1:].split()[0].lower()
        except Exception:
            return False
        return cmd in {"learn", "stop", "unmute", "clear", "clearcommands"}
    deleted_total = 0
    for ch in guild.text_channels:
        try:
            deleted = await ch.purge(limit=1000, check=is_command_msg)
            deleted_total += len(deleted)
        except Exception:
            pass
    await interaction.response.send_message(f"Deleted {deleted_total} command messages.", ephemeral=True)

# Voice state update event unchanged from your original code
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    guild = member.guild
    phase = guild_id_to_phase.get(guild.id)
    if not phase:
        return
    target_channel = None
    vc_id = guild_id_to_voice_channel_id.get(guild.id)
    if vc_id:
        ch = guild.get_channel(vc_id)
        if isinstance(ch, discord.VoiceChannel):
            target_channel = ch
    if target_channel is None:
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
            desired_mute = phase == "study"
            current_mute = after.mute
            if current_mute != desired_mute:
                now = time.time()
                key = (guild.id, member.id)
                recent_member_edit_time[key] = now
                await member.edit(mute=desired_mute, reason="Learning cycle server mute (join/update)")
        elif left_channel and target_channel and left_channel.id == target_channel.id and (not joined_channel or joined_channel.id != target_channel.id):
            if member.voice is not None and member.voice.mute:
                now = time.time()
                key = (guild.id, member.id)
                last = recent_member_edit_time.get(key, 0.0)
                if now - last >= PER_MEMBER_EDIT_COOLDOWN_SECONDS:
                    recent_member_edit_time[key] = now
                    await member.edit(mute=False, reason="Learning cycle cleanup (left channel)")
    except discord.Forbidden:
        pass

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Syncing commands...")
    try:
        # Sync commands for all guilds the bot is in (can be changed to specific guild for faster updating)
        await bot.tree.sync()
        print("Commands synced.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    print("------")

def _run():
    token = BOT_TOKEN
    if not token:
        print("No Discord token found. Set tokenbot environment variable.")
        return
    bot.run(token)

if __name__ == "__main__":
    _run()

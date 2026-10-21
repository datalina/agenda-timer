# Agenda Timer bot
# /timer posts a control panel with activity and duration menus and these buttons:
#   Start        starts the countdown; the bot joins the voice channel (if anyone is in it)
#   Pause/Resume pauses or resumes the countdown
#   +1 min       adds a minute; also works after time's up, but not after Done
#   Done         ends the activity early, with the chime and a "Done" message (bot stays in voice)
#   Move to bottom  reposts the panel below the latest messages
#   Exit timer   removes the panel and the bot leaves voice
# Buttons that can't be used at the moment are greyed out.
# /timer only works in a voice channel's chat, and the bot only ever joins that voice channel.
# The bot joins when someone enters the voice channel while a timer is running, and leaves
# when everyone else has left. If everyone leaves while a timer is running or paused, the
# activity is marked Incomplete with the time it ran. Only Exit timer removes a panel.
# When time runs out, the bot posts a "Time's up" message and plays the alert sound in the voice channel.
# To switch activities (or fix a wrong start), press Done, pick new settings, and press Start.
# Reads the bot token from token.txt in the same folder. Never put the token in this file.

import asyncio
import io
import math
import time
import wave
from pathlib import Path

import discord
from discord import app_commands

TOKEN = (Path(__file__).parent / "token.txt").read_text().strip()

# Sound played in the voice channel when time is up.
# To switch sounds, change the file name in quotes and restart the bot.
# Options in this folder: "chime.wav", "ava.wav", "timesup.wav"
# Any sound must be a WAV file: 48000 Hz, 16-bit, stereo.
ALERT_SOUND = Path(__file__).parent / "chime.wav"


def load_alert_sound():
    """Read the WAV file into memory. Returns None (with a printed reason) if it can't be used."""
    if not ALERT_SOUND.exists():
        print(f"No alert sound found at {ALERT_SOUND}. Voice alerts are off.")
        return None
    with wave.open(str(ALERT_SOUND), "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (48000, 2, 2):
            print(f"{ALERT_SOUND.name} must be 48000 Hz, 16-bit, stereo. Voice alerts are off.")
            return None
        return w.readframes(w.getnframes())


ALERT_PCM = load_alert_sound()

# Edit these lists to change the menus.
ACTIVITIES = [
    "Checking in / catching up",
    "Discussion",
    "Questions",
    "Planning / scheduling",
    "Transition",
    "Body doubling",
    "Group meeting",
]
DURATIONS = [1, 3, 5, 10, 15, 30, 45, 50, 60, 90]  # minutes
DEFAULT_MINUTES = 5


def fmt_minutes(m):
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} hr" + (f" {r} min" if r else "")


def fmt_seconds(s):
    s = max(0, int(s))
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}"


VOICE_CHANNEL_TYPES = (discord.VoiceChannel, discord.StageChannel)

# Every panel currently posted (cleared when the bot restarts).
PANELS = []


def people_in(channel):
    """True if anyone other than this bot is in the voice channel."""
    return any(uid != client.user.id for uid in channel.voice_states)


def panel_copies(*messages):
    """The distinct panel messages among those given (skips duplicates and missing ones)."""
    seen, result = set(), []
    for m in messages:
        if m is not None and m.id not in seen:
            seen.add(m.id)
            result.append(m)
    return result


class TimerPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.activity = ACTIVITIES[0]
        self.minutes = DEFAULT_MINUTES
        self.state = "ready"  # ready, running, paused, done
        self.end_time = None
        self.remaining = None
        self.task = None
        self.message = None
        self.voice_channel = None  # the voice channel whose chat this panel is in
        self.run_minutes = None  # duration chosen when Start was pressed
        self.extra_minutes = 0  # minutes added with +1 min since Start
        self.ended_early = False  # True after Done: the activity is closed and +1 min is greyed out
        self.used_at_end = 0  # seconds used when Done was pressed (or when everyone left)
        self.incomplete = False  # True if everyone left the voice channel while the timer was going
        self.last_action = None  # (what happened, who did it, when), shown at the bottom of the panel
        self.update_buttons()

    # ---------- display ----------
    def embed(self):
        e = discord.Embed(title=self.activity)
        if self.state == "running":
            end = int(self.end_time)
            e.description = f"⏳ Ends <t:{end}:R> (at <t:{end}:t>)"
            e.color = discord.Color.green()
        elif self.state == "paused":
            e.description = f"⏸️ Paused with {fmt_seconds(self.remaining)} remaining"
            e.color = discord.Color.orange()
        elif self.state == "done":
            if self.incomplete:
                e.description = "⚠️ Incomplete: everyone left the voice channel"
                e.color = discord.Color.dark_grey()
            else:
                e.description = "✅ Done" if self.ended_early else "⏰ Time's up"
                e.color = discord.Color.red()
        else:
            e.description = f"Ready: {fmt_minutes(self.minutes)}. Press Start."
            e.color = discord.Color.blurple()
        if self.last_action:
            action, user, when = self.last_action
            e.set_footer(text=f"{action} by {user.display_name}", icon_url=user.display_avatar.url)
            e.timestamp = when
        return e

    def update_buttons(self):
        # Greys out buttons that can't be used right now. The menus,
        # Move to bottom, and Exit timer are always available.
        timing = self.state in ("running", "paused")
        # Start is greyed out while a timer is running or paused, to prevent accidental resets.
        self.start.disabled = timing
        self.pause_resume.label = "Resume" if self.state == "paused" else "Pause"
        self.pause_resume.disabled = not timing
        self.end_now.disabled = not timing
        # +1 min also works after time's up, but not after Done or Incomplete (the activity is closed).
        closed = self.ended_early or self.incomplete
        self.add_minute.disabled = not (timing or (self.state == "done" and not closed))

    def note(self, interaction, action):
        # Records who pressed what, for the line at the bottom of the panel.
        self.last_action = (action, interaction.user, discord.utils.utcnow())

    async def refresh(self, interaction):
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ---------- timing ----------
    def start_watcher(self):
        if self.task:
            self.task.cancel()
        self.task = asyncio.create_task(self.watch())

    def stop_watcher(self):
        if self.task:
            self.task.cancel()
            self.task = None

    async def watch(self):
        # Sleeps until the end time; if +1 min moved the end time later, keeps waiting.
        while (delay := self.end_time - time.time()) > 0:
            await asyncio.sleep(delay)
        self.state = "done"
        self.task = None
        self.ended_early = False
        self.incomplete = False
        self.update_buttons()
        await self.message.edit(embed=self.embed(), view=self)
        await self.announce()

    async def announce(self):
        # Posts the end-of-activity message and plays the chime.
        # Shows only the total time the activity took:
        #   time ran out -> planned time plus any minutes added, e.g. "17 min"
        #   Done pressed -> time actually used, rounded up to whole minutes, e.g. "9 min"
        if self.ended_early:
            used = max(1, math.ceil(self.used_at_end / 60))
            text = f"✅ Done: **{self.activity}** ({fmt_minutes(used)})"
        else:
            total = self.run_minutes + self.extra_minutes
            text = f"⏰ Time's up: **{self.activity}** ({fmt_minutes(total)})"
        await self.message.channel.send(text)
        await self.play_alert()

    # ---------- voice ----------
    async def join_voice(self):
        # Joins the voice channel, or stays put if already there. Returns None if it can't.
        if self.voice_channel is None:
            return None
        try:
            vc = self.voice_channel.guild.voice_client
            if vc is None or not vc.is_connected():
                vc = await self.voice_channel.connect()
            elif vc.channel != self.voice_channel:
                await vc.move_to(self.voice_channel)
            return vc
        except Exception as e:
            print(f"Joining voice failed: {e!r}")
            return None

    async def leave_voice(self):
        vc = self.message.guild.voice_client
        if vc is not None:
            try:
                await vc.disconnect()
            except Exception as e:
                print(f"Leaving voice failed: {e!r}")

    async def mark_incomplete(self):
        # Everyone left the voice channel while the timer was running or paused:
        # stop the timer, post how long the activity ran, and grey out the timer buttons.
        if self.state == "running":
            left = self.end_time - time.time()
        elif self.state == "paused":
            left = self.remaining
        else:
            return
        self.stop_watcher()
        self.used_at_end = (self.run_minutes + self.extra_minutes) * 60 - left
        self.ended_early = False
        self.incomplete = True
        self.state = "done"
        self.update_buttons()
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except Exception as e:
            print(f"Couldn't update panel: {e!r}")
        used = max(1, math.ceil(self.used_at_end / 60))
        await self.message.channel.send(f"⚠️ Incomplete: **{self.activity}** ({fmt_minutes(used)})")

    async def close(self):
        # Removes this panel: stops its timer, deletes the message, and forgets it.
        self.stop_watcher()
        self.state = "ready"
        self.end_time = None
        if self in PANELS:
            PANELS.remove(self)
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"Couldn't delete panel: {e!r}")
        self.stop()  # tells discord.py this panel's buttons are no longer in use

    async def play_alert(self):
        # Plays the alert sound in the voice channel (rejoining if the bot got disconnected).
        # Any voice problem is printed in Command Prompt; the text alert still works.
        if ALERT_PCM is None or self.voice_channel is None:
            return
        # Skip the chime if nobody (other than this bot) is in the voice channel.
        if not people_in(self.voice_channel):
            return
        vc = await self.join_voice()
        if vc is None:
            return
        try:
            if vc.is_playing():
                vc.stop()
            vc.play(discord.PCMAudio(io.BytesIO(ALERT_PCM)))
        except Exception as e:
            print(f"Voice alert failed: {e!r}")

    # ---------- menus ----------
    @discord.ui.select(
        placeholder="Choose activity",
        row=0,
        options=[discord.SelectOption(label=a, default=(i == 0)) for i, a in enumerate(ACTIVITIES)],
    )
    async def pick_activity(self, interaction, select):
        self.activity = select.values[0]
        for o in select.options:
            o.default = o.label == self.activity
        self.note(interaction, "Changed activity")
        await self.refresh(interaction)

    @discord.ui.select(
        placeholder="Choose duration",
        row=1,
        options=[
            discord.SelectOption(label=fmt_minutes(m), value=str(m), default=(m == DEFAULT_MINUTES))
            for m in DURATIONS
        ],
    )
    async def pick_duration(self, interaction, select):
        self.minutes = int(select.values[0])
        for o in select.options:
            o.default = o.value == select.values[0]
        self.note(interaction, "Changed duration")
        await self.refresh(interaction)

    # ---------- buttons ----------
    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, row=2)
    async def start(self, interaction, button):
        if self.state in ("running", "paused"):
            await interaction.response.send_message(
                "A timer is already running. Press Done first.", ephemeral=True
            )
            return
        self.end_time = time.time() + self.minutes * 60
        self.state = "running"
        self.run_minutes = self.minutes
        self.extra_minutes = 0
        self.ended_early = False
        self.incomplete = False
        self.start_watcher()
        self.note(interaction, "Started")
        await self.refresh(interaction)
        # Join this panel's voice channel only if someone is in it.
        if people_in(self.voice_channel):
            asyncio.create_task(self.join_voice())

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, row=2)
    async def pause_resume(self, interaction, button):
        if self.state == "running":
            self.remaining = self.end_time - time.time()
            self.stop_watcher()
            self.state = "paused"
            self.note(interaction, "Paused")
        elif self.state == "paused":
            self.end_time = time.time() + self.remaining
            self.state = "running"
            self.start_watcher()
            self.note(interaction, "Resumed")
        else:
            await interaction.response.send_message("No timer is running.", ephemeral=True)
            return
        await self.refresh(interaction)

    @discord.ui.button(label="+1 min", style=discord.ButtonStyle.secondary, row=2)
    async def add_minute(self, interaction, button):
        if self.state == "running":
            self.end_time += 60
        elif self.state == "paused":
            self.remaining += 60
        elif self.state == "done" and not (self.ended_early or self.incomplete):
            # After time's up, +1 min gives the same activity one more minute.
            self.end_time = time.time() + 60
            self.state = "running"
            self.start_watcher()
        elif self.state == "done":
            await interaction.response.send_message(
                "This activity is done. Choose the next one and press Start.", ephemeral=True
            )
            return
        else:
            await interaction.response.send_message("Press Start first.", ephemeral=True)
            return
        self.extra_minutes += 1
        self.note(interaction, "+1 min")
        await self.refresh(interaction)

    @discord.ui.button(label="Done", emoji="🔔", style=discord.ButtonStyle.primary, row=2)
    async def end_now(self, interaction, button):
        # Finishes the activity early, with the chime and a "Done" message.
        if self.state == "running":
            left = self.end_time - time.time()
        elif self.state == "paused":
            left = self.remaining
        else:
            await interaction.response.send_message("No timer is running.", ephemeral=True)
            return
        self.stop_watcher()
        self.used_at_end = (self.run_minutes + self.extra_minutes) * 60 - left
        self.ended_early = True
        self.state = "done"
        self.note(interaction, "Done early")
        await self.refresh(interaction)
        await self.announce()

    @discord.ui.button(label="Move to bottom", emoji="⬇️", style=discord.ButtonStyle.secondary, row=3)
    async def move_to_bottom(self, interaction, button):
        # Reposts this panel below the latest messages and deletes the old copy.
        # The timer keeps running; nothing about it changes.
        await interaction.response.defer()
        old_copies = panel_copies(interaction.message, self.message)
        self.message = await interaction.channel.send(embed=self.embed(), view=self)
        for old in old_copies:
            try:
                await old.delete()
            except discord.NotFound:
                pass  # already gone
            except Exception as e:
                print(f"Couldn't delete old panel: {e!r}")

    @discord.ui.button(label="Exit timer", style=discord.ButtonStyle.danger, row=3)
    async def exit_timer(self, interaction, button):
        # Removes the panel. The bot leaves voice unless another panel in this
        # voice channel still has a timer running or paused.
        await interaction.response.defer()
        if interaction.message is not None and (self.message is None or interaction.message.id != self.message.id):
            try:
                await interaction.message.delete()  # an older copy, just in case
            except Exception:
                pass
        await self.close()
        others_timing = any(
            p.voice_channel == self.voice_channel and p.state in ("running", "paused") for p in PANELS
        )
        if not others_timing:
            await self.leave_voice()


client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)


@tree.command(name="timer", description="Post a timer control panel in this channel")
async def timer(interaction: discord.Interaction):
    # Only works in a voice channel's chat, so the bot always joins the right voice channel.
    if not isinstance(interaction.channel, VOICE_CHANNEL_TYPES):
        await interaction.response.send_message(
            "Use /timer in a voice channel's chat (the speech-bubble icon in the call).", ephemeral=True
        )
        return
    panel = TimerPanel()
    panel.voice_channel = interaction.channel
    panel.message = await interaction.channel.send(embed=panel.embed(), view=panel)
    PANELS.append(panel)
    await interaction.response.send_message("Timer panel posted.", ephemeral=True)


@client.event
async def on_voice_state_update(member, before, after):
    # Runs whenever someone joins, leaves, or switches voice channels.
    if member.id == client.user.id:
        return  # ignore the bot's own moves

    # Someone joined a voice channel where a timer is running: the bot joins too.
    joined = after.channel
    if joined is not None and joined != before.channel:
        for panel in PANELS:
            if panel.voice_channel == joined and panel.state in ("running", "paused"):
                await panel.join_voice()
                break

    # The last person left a voice channel: the bot leaves too, and any timer
    # running or paused there is marked Incomplete. Panels are never deleted here.
    left = before.channel
    if left is None or left == after.channel or people_in(left):
        return
    vc = member.guild.voice_client
    if vc is not None and vc.is_connected() and vc.channel == left:
        try:
            await vc.disconnect()
        except Exception as e:
            print(f"Leaving empty voice channel failed: {e!r}")
    for panel in list(PANELS):
        if panel.voice_channel == left and panel.state in ("running", "paused"):
            await panel.mark_incomplete()


@client.event
async def on_ready():
    await tree.sync()
    # Clear any leftover voice presence from before a restart (e.g. if the window was closed abruptly).
    for guild in client.guilds:
        if guild.me.voice is not None:
            try:
                await guild.change_voice_state(channel=None)
            except Exception as e:
                print(f"Couldn't clear old voice connection in {guild.name}: {e!r}")
    print(f"Logged in as {client.user}. Press Ctrl+C to stop the bot.")


client.run(TOKEN)

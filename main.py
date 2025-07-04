import os
import re
import io
import subprocess
import sys
import httpx
import logging
import asyncio
import requests
import aiohttp
from collections import deque
from dotenv import load_dotenv
import logging
import replicate
import pathlib
import serverig
import requests

logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Auto-install PyNaCl if missing
try:
    import nacl
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pynacl"])
    import nacl

import discord
from discord.ext import commands
import base64
from discord import app_commands
from discord import Interaction, User, app_commands
from httpx import AsyncClient
from httpx import HTTPStatusError
import yt_dlp
from io import BytesIO

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

# Enhanced logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce discord library logging
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('discord.voice_state').setLevel(logging.WARNING)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

WEBHOOK_URL = os.getenviron["BAN_WEBHOOK_URL"]
ALLOWED_USER_IDS = {
    1134133832023560215, # bob
    886638615629811773, # ed
    889182558905069609, # nubi
    944296234263384104, # tabby
    1012767514507362315, # mono
    883897095445160047, # the nub
    771319425151795200, # vox

    # fake mods (they're mod in scammer server) but theiir sbtd communuity memebers so yeah cool
    939111471613370389, # citrus fluff
    553573360508993557, # l another furry (united behavior)
    584603465431515156, # archadium

    # l scammers
    1012549777319280700, # matrix
    1384444338531991563, # goth
    390715906449211393, # febeary
    642498940918431755, # jovan who tf is this
    1061958304684834826, # the "owner" with the gay ah tag
    1267571976445235300, # who tf is this (bro has admin role)
    864958103103078470, # "lol"
    1370566192280109098 # 2nd owner ig (this is bros fake gf i bet)
}

# Enhanced bot configuration
bot = commands.Bot(
    command_prefix="?",
    intents=intents,
    reconnect=True,
    enable_debug_events=False,
    heartbeat_timeout=60.0,  # Increased heartbeat timeout
    max_messages=None  # Disable message cache to save memory
)

current_player = {}  # guild.id -> user.id
music_queues = {}  # guild.id -> deque of track info
currently_playing = {}  # guild.id -> current track info
connection_retries = {}  # guild.id -> retry count

# Enhanced connection settings
CONNECT_DELAY = 3  # Reduced delay
DISCONNECT_DELAY = 1
MAX_RETRIES = 5  # Increased max retries
RETRY_DELAY = 2  # Base delay between retries

youtube_regex = re.compile(
    r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$'
)


class TrackInfo:
    def __init__(self, url, title, user_id, user_name, duration=None, filename=None):
        self.url = url
        self.title = title
        self.user_id = user_id
        self.user_name = user_name
        self.duration = duration
        self.filename = filename


def format_duration(seconds):
    """Convert seconds to MM:SS format"""
    if not seconds:
        return "Unknown"

    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    return f"{minutes}:{seconds:02d}"


def get_queue(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = deque()
    return music_queues[guild_id]


async def cleanup_voice_client(guild_id):
    """Clean up voice client and related data"""
    voice_client = discord.utils.get(bot.voice_clients, guild=bot.get_guild(guild_id))

    if voice_client and voice_client.is_connected():
        try:
            if voice_client.is_playing():
                voice_client.stop()
            await voice_client.disconnect(force=True)
            logger.info(f"Cleaned up voice client for guild {guild_id}")
        except Exception as e:
            logger.error(f"Error cleaning up voice client for guild {guild_id}: {e}")

    # Clean up data
    current_player.pop(guild_id, None)
    currently_playing.pop(guild_id, None)
    connection_retries.pop(guild_id, None)
    if guild_id in music_queues:
        music_queues[guild_id].clear()


async def connect_to_voice_channel(channel, max_retries=MAX_RETRIES):
    """Enhanced voice connection with exponential backoff"""
    guild_id = channel.guild.id

    for attempt in range(max_retries):
        try:
            logger.info(f"Attempting to connect to {channel} (attempt {attempt + 1}/{max_retries})")

            # Exponential backoff delay
            if attempt > 0:
                delay = RETRY_DELAY * (2 ** (attempt - 1))
                logger.info(f"Waiting {delay} seconds before retry...")
                await asyncio.sleep(delay)

            # Try to connect with timeout
            voice_client = await asyncio.wait_for(
                channel.connect(reconnect=False, timeout=30.0),
                timeout=35.0
            )

            logger.info(f"Successfully connected to {channel}")
            connection_retries[guild_id] = 0  # Reset retry counter

            # Brief delay to ensure connection is stable
            await asyncio.sleep(CONNECT_DELAY)
            return voice_client

        except asyncio.TimeoutError:
            logger.warning(f"Connection timeout for {channel} (attempt {attempt + 1})")
        except discord.errors.ConnectionClosed as e:
            logger.warning(f"Connection closed for {channel}: {e} (attempt {attempt + 1})")
        except Exception as e:
            logger.error(f"Unexpected error connecting to {channel}: {e} (attempt {attempt + 1})")

        # Store retry count
        connection_retries[guild_id] = attempt + 1

    logger.error(f"Failed to connect to {channel} after {max_retries} attempts")
    return None


async def should_disconnect(guild_id, user_who_left_id=None):
    """Check if bot should disconnect based on queue state and user departures"""
    queue = get_queue(guild_id)
    current_track = currently_playing.get(guild_id)
    host_id = current_player.get(guild_id)

    # Always disconnect if queue is empty and nothing playing
    if not current_track and not queue:
        return True

    # If the HOST left the VC AND only host songs remain, disconnect
    if user_who_left_id and host_id and user_who_left_id == host_id:
        host_songs_only = True

        # Check current track
        if current_track and current_track.user_id != host_id:
            host_songs_only = False

        # Check queue
        if host_songs_only:
            for track in queue:
                if track.user_id != host_id:
                    host_songs_only = False
                    break

        if host_songs_only:
            return True

    # If someone left, check if we should disconnect
    if user_who_left_id:
        # If only one item total (current track) and that person left
        if current_track and not queue and current_track.user_id == user_who_left_id:
            return True

        # If only one item in queue and that person left (and nothing currently playing)
        if not current_track and len(queue) == 1 and queue[0].user_id == user_who_left_id:
            return True

    return False


async def play_next_track(guild, voice_client):
    queue = get_queue(guild.id)

    # Enhanced voice client validation
    if not voice_client:
        logger.warning("Voice client is None")
        await cleanup_voice_client(guild.id)
        return

    if not voice_client.is_connected():
        logger.warning("Voice client is not connected")
        await cleanup_voice_client(guild.id)
        return

    # Check if we should disconnect
    if await should_disconnect(guild.id):
        logger.info("Should disconnect - queue empty or host left")
        await asyncio.sleep(DISCONNECT_DELAY)

        # Check again in case something was added during the delay
        if await should_disconnect(guild.id):
            await cleanup_voice_client(guild.id)
            logger.info("Disconnected - queue empty or host left")
        return

    if not queue:
        logger.info("Queue is empty, nothing to play")
        return

    track = queue.popleft()
    currently_playing[guild.id] = track

    # Fixed yt-dlp options - stream directly instead of downloading
    ydl_opts = {
        "format": "bestaudio[ext=webm]/bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractaudio": False,  # Don't extract audio, stream directly
        "audioformat": "best",
        "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    }

    try:
        logger.info(f"Getting stream URL for track: {track.title}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track.url, download=False)
            # Get the direct stream URL
            if 'url' in info:
                stream_url = info['url']
            elif 'formats' in info and info['formats']:
                # Find best audio format
                audio_formats = [f for f in info['formats'] if f.get('acodec') != 'none']
                if audio_formats:
                    stream_url = audio_formats[0]['url']
                else:
                    stream_url = info['formats'][0]['url']
            else:
                raise Exception("Could not extract stream URL")

            track.filename = stream_url  # Store the stream URL
            logger.info(f"Got stream URL: {stream_url[:100]}...")

    except Exception as e:
        logger.error(f"Failed to get stream URL: {e}")
        # Try to play next track
        await play_next_track(guild, voice_client)
        return

    def after_playing(error):
        if error:
            logger.error(f"Player error: {error}")

        # No file cleanup needed since we're streaming

        # Schedule next track
        fut = asyncio.run_coroutine_threadsafe(play_next_track(guild, voice_client), bot.loop)
        try:
            fut.result()
        except Exception as e:
            logger.error(f"Error in next track task: {e}")

    try:
        # Fixed FFmpeg options - removed invalid options
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'  # Removed invalid audio bitrate option for streaming
        }

        logger.info(f"Starting playback: {track.title}")
        # Use the stream URL directly
        voice_client.play(
            discord.FFmpegPCMAudio(source=stream_url, **ffmpeg_options),
            after=after_playing
        )
        logger.info("Playback started successfully")

    except Exception as e:
        logger.error(f"Error starting playback: {e}")
        # Try next track
        await play_next_track(guild, voice_client)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}!")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")


@bot.event
async def on_disconnect():
    logger.warning("Bot disconnected from Discord")


@bot.event
async def on_resumed():
    logger.info("Bot connection resumed")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    guild = member.guild
    host_id = current_player.get(guild.id)
    voice_client = discord.utils.get(bot.voice_clients, guild=guild)

    # Enhanced voice client disconnect handling
    if voice_client and not voice_client.is_connected():
        logger.warning(f"Voice client disconnected unexpectedly in guild {guild.id}")
        if voice_client.is_playing():
            voice_client.stop()
        await cleanup_voice_client(guild.id)
        return

    # If the user who started playback left or switched channels
    if host_id and member.id == host_id:
        if before.channel is not None and after.channel != before.channel:
            await asyncio.sleep(1)  # slight delay for state to settle
            if voice_client and voice_client.is_connected():
                # Check if we should disconnect based on new logic
                if await should_disconnect(guild.id, member.id):
                    await cleanup_voice_client(guild.id)
                    logger.info(f"Host {member} left VC, bot disconnected")

BLACKLIST = {
    69
}

@bot.tree.command(name="roast-v2", description="ez")
@app_commands.describe(user="the user idk")
async def roast(interaction: Interaction, user: User):
    if user.id in BLACKLIST:
        await interaction.response.send_message(
            f"haha {interaction.user.mention} tried to roast **{user.display_name}** but instead failed but bc bro is gay 💀💀💀",
        )
        return

    await interaction.response.defer()

    prompt = (
        f"roast a Discord user named '{user.display_name}'. "
        "be offenive, edgy, and funny, and use popular emojis in the list, like the skull emoji or the rose emoji for examples"
        "use the username multiple times and make just ONE line and just provide the roast, no extra"
    )

    # Ollama format:
    payload = {
        "model": "llama3",
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.9,
            "num_predict": 120
        }
    }

    retry_attempts = 3
    delay = 2

    for attempt in range(retry_attempts):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Try different common endpoints based on your service:
                # Ollama: http://localhost:11434/api/generate
                # LM Studio: http://localhost:1234/v1/completions
                # Text Generation WebUI: http://localhost:7860/api/v1/generate

                response = await client.post("http://localhost:11434/api/generate", json=payload)  # Ollama default

                # Debug: print status and response
                print(f"Status: {response.status_code}")
                print(f"Response: {response.text}")

                response.raise_for_status()
                data = response.json()

                # Try different response formats
                roast_text = None
                if "results" in data and len(data["results"]) > 0:
                    roast_text = data["results"][0].get("text")
                elif "choices" in data and len(data["choices"]) > 0:
                    roast_text = data["choices"][0].get("text") or data["choices"][0].get("message", {}).get("content")
                elif "response" in data:
                    roast_text = data["response"]
                elif "text" in data:
                    roast_text = data["text"]
                elif "content" in data:
                    roast_text = data["content"]

                if roast_text and roast_text.strip():
                    await interaction.followup.send(roast_text.strip())
                    return
                else:
                    print(f"No valid text found in response: {data}")

        except httpx.HTTPStatusError as e:
            print(f"HTTP Error {e.response.status_code}: {e.response.text}")
            if e.response.status_code == 429:
                print(f"Rate limited, retrying in {delay} seconds (attempt {attempt + 1})")
                await asyncio.sleep(delay)
                delay *= 2
            else:
                await interaction.followup.send(
                    f"dumah {interaction.user.mention} failed to roast {user.display_name} 💀💀💀 (HTTP {e.response.status_code})",
                )
                return

        except httpx.ConnectError as e:
            print(f"Connection failed (attempt {attempt + 1}): {e}")
            if attempt < retry_attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2

        except Exception as e:
            print(f"Unexpected error: {e}")
            await interaction.followup.send(
                f"dumah {interaction.user.mention} failed to roast {user.display_name} 💀💀💀: {type(e).__name__}",
            )
            return

    # If all attempts failed
    await interaction.followup.send(
        f"dumah {interaction.user.mention} failed to roast {user.display_name} 💀💀💀 (service unavailable)",
    )


HUGGINGFACE_API_KEY = os.getenviron["HUGGINGFACE_API_KEY"]

# Assuming you have a bot instance
# bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

@bot.tree.command(name="generate-image", description="creates a ass ah art pls dont warn me")
@app_commands.describe(prompt="idk u do it")
async def generate_image(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()

    # Hugging Face Stable Diffusion API
    api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
    headers = {"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"}

    # Simplified payload - HF Inference API has limited parameter support
    payload = {
        "inputs": f"{prompt}, high quality, detailed, sharp, professional, masterpiece, best quality, ultra detailed, 8k resolution",
        "parameters": {
            "num_inference_steps": 30,  # Reduced for faster generation
            "guidance_scale": 7.5,  # Standard value
        }
    }

    retry_attempts = 3
    delay = 10

    for attempt in range(retry_attempts):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:  # Increased timeout
                response = await client.post(api_url, headers=headers, json=payload)

                print(f"Status: {response.status_code}")

                # Check for specific status codes
                if response.status_code == 503:
                    # Model is loading, wait and retry
                    print(f"Model loading, retrying in {delay} seconds...")
                    await interaction.followup.send(
                        f"afjadsfknasdfuasdnf trying again in {delay} seconds 💀💀💀 (attempt {attempt + 1}/3)",
                    )
                    await asyncio.sleep(delay)
                    delay += 15
                    continue

                elif response.status_code == 429:
                    # Rate limited
                    await interaction.followup.send(
                        f"llll imagine spamming the bot 💀💀💀 {interaction.user.mention} ez",
                    )
                    return

                elif response.status_code == 401:
                    # Unauthorized
                    await interaction.followup.send(
                        "idk you'll never see this",
                    )
                    return

                elif response.status_code == 400:
                    # Bad request
                    print(f"Bad request: {response.text}")
                    await interaction.followup.send(
                        f"lll prompt failed somehow {interaction.user.mention} is gay btw 💀💀💀💀",
                    )
                    return

                # Check if request was successful
                if response.status_code == 200:
                    # The response should be image bytes
                    image_bytes = response.content

                    # Validate we got image data
                    if len(image_bytes) > 0 and image_bytes.startswith(b'\x89PNG') or image_bytes.startswith(
                            b'\xff\xd8\xff'):
                        # Create Discord file from image bytes
                        image_file = discord.File(
                            io.BytesIO(image_bytes),
                            filename=f"generated_image_{interaction.user.id}.png"
                        )

                        # Send the image
                        await interaction.followup.send(
                            f"heres the ass ah art that {interaction.user.mention} created also heres the prompt: {prompt}",
                            file=image_file
                        )
                        return
                    else:
                        print(f"Invalid image data received. Length: {len(image_bytes)}")
                        print(f"First few bytes: {image_bytes[:20]}")
                        # Try to parse as JSON error message
                        try:
                            error_data = response.json()
                            print(f"API Error: {error_data}")
                            await interaction.followup.send(
                                f"lll api returned a err {interaction.user.mention} is gay btw 💀💀💀: {error_data.get('error', 'Unknown error')}",
                            )
                            return
                        except:
                            pass
                else:
                    # Other HTTP errors
                    print(f"HTTP Error {response.status_code}: {response.text}")
                    if attempt < retry_attempts - 1:
                        await asyncio.sleep(delay)
                        delay += 10
                    else:
                        await interaction.followup.send(
                            f"idk what to even say at this point why tf ai makes things cringe {response.status_code}). Please try again later.",
                        )
                        return

        except httpx.TimeoutException:
            print(f"Request timeout (attempt {attempt + 1})")
            if attempt < retry_attempts - 1:
                await interaction.followup.send(
                    f"lll it somehow timed out 💀💀. (attempt {attempt + 1}/3)",
                )
                await asyncio.sleep(delay)
                delay += 10
            else:
                await interaction.followup.send(
                    f"haha {interaction.user.mention} is gay so it failed 💀💀💀",
                )
                return

        except httpx.ConnectError as e:
            print(f"Connection failed (attempt {attempt + 1}): {e}")
            if attempt < retry_attempts - 1:
                await asyncio.sleep(delay)
                delay += 10
            else:
                await interaction.followup.send(
                    "💀💀💀💀",
                )
                return

        except Exception as e:
            print(f"Unexpected error: {e}")
            await interaction.followup.send(
                f"💀💀💀💀 {type(e).__name__}",
                ephemeral=True
            )
            return

    # If all attempts failed
    await interaction.followup.send(
        "💀💀💀💀💀💀💀.",
        ephemeral=True
    )

@bot.tree.command(name="sbtd-ban", description="bans someone from sbtd but u gotta be a mod to do this so skill issue (real)")
@app_commands.describe(
    username="the roblox user to ban",
    duration="ban duration (1m, 1h, 1d, 1w, 1y)",
    reason="self explaintory"
)
async def ban_command(interaction: discord.Interaction, username: str, duration: str, reason: str):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("bros not a mod 💀💀", ephemeral=True)
        return

    duration = duration.lower()
    if not any(duration.endswith(suffix) for suffix in ["m", "h", "d", "w", "y"]):
        duration = 0

    payload = {
        "username": username,
        "duration": duration,
        "reason": reason,
        "mod": interaction.user.id,
        "token": os.getenviron["BAN_TOKEN"]
    }

    try:
        response = requests.post(WEBHOOK_URL, json=payload)
        if response.status_code == 200:
            await interaction.response.send_message(f"the dumbah `{username}` has been banned from sbtd 💀")
        else:
            await interaction.response.send_message(f"skill issue: {response.status_code}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"request err: 💀💀💀 {e}", ephemeral=True)

@bot.tree.command(name="play", description="plays a yt video in ur vc that ur currently in")
@app_commands.describe(url="a youtube video url (duh)")
async def play_command(interaction: discord.Interaction, url: str):
    voice_state = interaction.user.voice

    if not voice_state or not voice_state.channel:
        await interaction.response.send_message(
            "bro didnt join a vc 💀", ephemeral=True
        )
        return

    if not youtube_regex.match(url):
        await interaction.response.send_message(
            "l imagine putting smth else in the prompt 💀", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Extract video info first
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', None)
    except Exception as e:
        await interaction.followup.send(f"failed to get video info: {e}", ephemeral=True)
        return

    track = TrackInfo(url, title, interaction.user.id, interaction.user.display_name, duration)
    queue = get_queue(interaction.guild.id)
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    vc_channel = voice_state.channel

    # If nothing is playing, start immediately
    if not voice_client or not voice_client.is_playing():
        current_player[interaction.guild.id] = interaction.user.id

        try:
            if voice_client and voice_client.is_connected():
                if voice_client.channel != vc_channel:
                    logger.info(f"Moving voice client to {vc_channel}")
                    await voice_client.move_to(vc_channel)
                else:
                    logger.info(f"Voice client already connected to {vc_channel}")
            else:
                logger.info(f"Connecting to voice channel {vc_channel}...")
                voice_client = await connect_to_voice_channel(vc_channel)

                if not voice_client:
                    retry_count = connection_retries.get(interaction.guild.id, 0)
                    await interaction.followup.send(
                        f"failed to connect to VC after {retry_count} attempts. "
                        f"skill issue ngl 💀",
                        ephemeral=True
                    )
                    return

        except Exception as e:
            logger.error(f"Unexpected error connecting to VC: {e}")
            await interaction.followup.send(f"failed to connect to VC: {e}", ephemeral=True)
            return

        # Add to queue and play immediately
        queue.append(track)
        await interaction.followup.send(f"ight ima play: **{title}**", ephemeral=True)
        await play_next_track(interaction.guild, voice_client)
    else:
        # Add to queue
        queue.append(track)
        queue_position = len(queue)
        await interaction.followup.send(f"added to queue (#{queue_position}): **{title}**", ephemeral=True)


@bot.tree.command(name="skip",
                  description="skips to the next track (requires manage messages permission or being the host)")
async def skip_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected() or not voice_client.is_playing():
        await interaction.response.send_message("nothing is playing 💀💀💀", ephemeral=True)
        return

    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Check if user can skip:
    # 1. Has manage messages permission
    # 2. Is the host and current song is their own
    can_skip = False

    if perms.manage_messages:
        can_skip = True
    elif host_id == interaction.user.id and current_track and current_track.user_id == interaction.user.id:
        can_skip = True

    if not can_skip:
        await interaction.response.send_message(
            "this server aint no playground 💯💯",
            ephemeral=True)
        return

    track_name = current_track.title if current_track else "current track"

    voice_client.stop()  # This will trigger the after_playing callback which plays next track
    await interaction.response.send_message(f"skipped: **{track_name}**", ephemeral=True)


@bot.tree.command(name="leave", description="makes the bot leave the voice channel")
async def leave_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("bro tried to leave without joining 💀", ephemeral=True)
        return

    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Check if there's music playing or in queue
    has_music = current_track is not None or len(queue) > 0

    # If there's music and user doesn't have manage messages, deny
    if has_music and not perms.manage_messages:
        await interaction.response.send_message(
            "bro tried to make the bot leave 💀💀", ephemeral=True)
        return

    # Use enhanced cleanup
    await cleanup_voice_client(interaction.guild.id)
    await interaction.response.send_message("ight ima leave this is a l vc only noobi is here 💀💀", ephemeral=True)


@bot.tree.command(name="stop", description="stops the current audio thats playing in a vc")
async def stop_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected() or not voice_client.is_playing():
        await interaction.response.send_message("l nothing is playing 💀", ephemeral=True)
        return

    player_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Check if user is the player OR has Move Members permission
    if player_id != interaction.user.id and not perms.move_members:
        await interaction.response.send_message("bro tried to stop it without starting it 💀💀💀💀", ephemeral=True)
        return

    # Check if there are other people's songs in the queue
    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)

    # Check if current track belongs to someone else
    other_peoples_songs = False
    if current_track and current_track.user_id != interaction.user.id:
        other_peoples_songs = True

    # Check if queue has other people's songs
    if not other_peoples_songs:
        for track in queue:
            if track.user_id != interaction.user.id:
                other_peoples_songs = True
                break

    # If there are other people's songs and user doesn't have manage messages, deny
    if other_peoples_songs and not perms.manage_messages:
        await interaction.response.send_message(
            "bro tried to stop with songs pending 💀💀💀",
            ephemeral=True)
        return

    # Clear the queue and stop
    queue.clear()

    voice_client.stop()
    await interaction.response.send_message("ok fine ima stop", ephemeral=True)


class QueueView(discord.ui.View):
    def __init__(self, user_id, queue):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.queue = list(queue)
        self.current_page = 0
        self.max_per_page = 5
        self.total_pages = (len(self.queue) - 1) // self.max_per_page + 1 if self.queue else 1
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.total_pages > 1:
            if self.current_page > 0:
                self.add_item(self.prev_button)
            if self.current_page < self.total_pages - 1:
                self.add_item(self.next_button)

    def _get_page_items(self):
        start = self.current_page * self.max_per_page
        end = start + self.max_per_page
        return self.queue[start:end]

    async def update_message(self, interaction):
        page_tracks = self._get_page_items()
        embed = discord.Embed(title="🗑️ garbage can queue 💀", color=0x00ff00)
        total_tracks = len(self.queue)
        embed.description = f"page {self.current_page + 1}/{self.total_pages} — total tracks: {total_tracks}\n\n"

        for i, track in enumerate(page_tracks, start=self.current_page * self.max_per_page + 1):
            embed.description += f"**{i}. {track.title}** — requested by <@{track.user_id}> — length: {format_duration(track.duration)}\n"

        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="previous", style=discord.ButtonStyle.primary, row=0)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("bro clicked on another bro's menu 💀", ephemeral=True)
            return
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_message(interaction)

    @discord.ui.button(label="next", style=discord.ButtonStyle.primary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("bro clicked on another bro's menu 💀", ephemeral=True)
            return
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await self.update_message(interaction)


@bot.tree.command(name="queue", description="shows ur garbage ah sounds and videos in a queue 💀")
async def queue_command(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)

    if not current_track and not queue:
        await interaction.response.send_message("theres nothing in the queue thank fucking god 💀💀💀", ephemeral=True)
        return

    # Create initial embed
    embed = discord.Embed(title="🗑️ garbage can queue 💀", color=0x00ff00)

    # Show currently playing track
    if current_track:
        embed.add_field(
            name="now playing: 🗑️💀",
            value=f"**{current_track.title}** — requested by <@{current_track.user_id}> — length: {format_duration(current_track.duration)}",
            inline=False,
        )

    # Show queue items if any exist
    if queue:
        view = QueueView(interaction.user.id, queue)
        page_tracks = view._get_page_items()
        total_tracks = len(queue)

        queue_text = f"page {view.current_page + 1}/{view.total_pages} — total tracks: {total_tracks}\n\n"

        for i, track in enumerate(page_tracks, start=view.current_page * view.max_per_page + 1):
            queue_text += f"**{i}. {track.title}** — requested by <@{track.user_id}> — length: {format_duration(track.duration)}\n"

        embed.add_field(
            name="up next:",
            value=queue_text,
            inline=False
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    else:
        # No queue items, just show currently playing
        await interaction.response.send_message(embed=embed, ephemeral=True)

class TrackRemovalView(discord.ui.View):
    def __init__(self, user_id, queue, user_tracks):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.queue = queue
        self.user_tracks = user_tracks

        # Add buttons for each track (max 5 due to Discord limits)
        for i, (queue_pos, track) in enumerate(user_tracks[:5]):
            button = discord.ui.Button(
                label=f"{i + 1}. {track.title[:30]}{'...' if len(track.title) > 30 else ''}",
                style=discord.ButtonStyle.red,
                custom_id=f"remove_{i}"
            )
            button.callback = self.create_remove_callback(i)
            self.add_item(button)

    def create_remove_callback(self, track_index):
        async def remove_callback(interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("bro tried to click another bro's menu 💀", ephemeral=True)
                return

            queue_pos, track = self.user_tracks[track_index]

            # Convert deque to list, remove item, convert back
            queue_list = list(self.queue)
            # Adjust index for any previously removed items
            actual_index = queue_pos
            for other_pos, _ in self.user_tracks[:track_index]:
                if other_pos < queue_pos:
                    actual_index -= 1

            if actual_index < len(queue_list):
                removed_track = queue_list.pop(actual_index)
                self.queue.clear()
                self.queue.extend(queue_list)

                await interaction.response.send_message(f"removed from queue ✅✅✅: **{removed_track.title}**",
                                                        ephemeral=True)
            else:
                await interaction.response.send_message("track not found in queue skill issue ngl 💀",
                                                        ephemeral=True)

        return remove_callback


if token:
    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
else:
    logger.error("ERROR: DISCORD_TOKEN not found in environment variables.")

serverig.keep_alive()

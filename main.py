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
import typing

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
from typing import Optional
from discord import Interaction, User, app_commands
from httpx import AsyncClient
from httpx import HTTPStatusError
import yt_dlp
from io import BytesIO

load_dotenv()
token = os.environ.get("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

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

WEBHOOK_URL = os.environ.get("BAN_WEBHOOK_URL")
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


# Enhanced cleanup function
async def cleanup_voice_client(guild_id):
    """Enhanced cleanup with better error handling"""
    try:
        voice_client = discord.utils.get(bot.voice_clients, guild=bot.get_guild(guild_id))

        if voice_client:
            try:
                if voice_client.is_playing():
                    voice_client.stop()
                    await asyncio.sleep(0.5)  # Wait for stop to process
                
                if voice_client.is_connected():
                    await voice_client.disconnect(force=True)
                    logger.info(f"Cleaned up voice client for guild {guild_id}")
            except Exception as e:
                logger.error(f"Error disconnecting voice client: {e}")

        # Clean up data
        current_player.pop(guild_id, None)
        currently_playing.pop(guild_id, None)
        connection_retries.pop(guild_id, None)
        if guild_id in music_queues:
            music_queues[guild_id].clear()

    except Exception as e:
        logger.error(f"Error in cleanup_voice_client: {e}")


async def voice_health_check():
    """Periodically check voice connection health"""
    while True:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds
            
            for voice_client in bot.voice_clients:
                if voice_client.is_connected():
                    # Check if connection is healthy
                    if not voice_client.is_playing() and not get_queue(voice_client.guild.id):
                        # No music and no queue, might want to disconnect after a while
                        pass
                else:
                    # Connection is broken, clean up
                    logger.warning(f"Found broken voice connection for guild {voice_client.guild.id}")
                    await cleanup_voice_client(voice_client.guild.id)
                    
        except Exception as e:
            logger.error(f"Error in voice health check: {e}")
            await asyncio.sleep(60)  # Wait longer on error

# Add error handler for voice state updates
@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes and cleanup"""
    if member.bot:
        return

    # Check if someone left a voice channel where the bot is connected
    if before.channel and not after.channel:
        voice_client = discord.utils.get(bot.voice_clients, guild=before.channel.guild)
        if voice_client and voice_client.channel == before.channel:
            # Check if we should disconnect
            if await should_disconnect(before.channel.guild.id, member.id):
                logger.info(f"User {member.display_name} left, checking if should disconnect")
                await asyncio.sleep(5)  # Wait a bit in case they rejoin
                if await should_disconnect(before.channel.guild.id, member.id):
                    await cleanup_voice_client(before.channel.guild.id)
                    logger.info("Disconnected due to user leaving")

# Enhanced connection function with better error handling
async def connect_to_voice_channel(channel, max_retries=3):
    """Enhanced voice connection with better error handling and backoff"""
    guild_id = channel.guild.id

    for attempt in range(max_retries):
        try:
            logger.info(f"Attempting to connect to {channel} (attempt {attempt + 1}/{max_retries})")

            # Progressive delay between retries
            if attempt > 0:
                delay = min(2 ** attempt, 16)  # Cap at 16 seconds
                logger.info(f"Waiting {delay} seconds before retry...")
                await asyncio.sleep(delay)

            # Clean up any existing connection first
            existing_client = discord.utils.get(bot.voice_clients, guild=channel.guild)
            if existing_client:
                try:
                    await existing_client.disconnect(force=True)
                    await asyncio.sleep(2)  # Wait for cleanup
                except:
                    pass

            # Try to connect with shorter timeout
            voice_client = await asyncio.wait_for(
                channel.connect(reconnect=True, timeout=20.0),
                timeout=25.0
            )

            logger.info(f"Successfully connected to {channel}")
            connection_retries[guild_id] = 0

            # Brief stability check
            await asyncio.sleep(1)
            
            if voice_client.is_connected():
                return voice_client
            else:
                logger.warning("Voice client connected but not stable")
                await voice_client.disconnect(force=True)
                continue

        except asyncio.TimeoutError:
            logger.warning(f"Connection timeout for {channel} (attempt {attempt + 1})")
        except discord.errors.ConnectionClosed as e:
            logger.warning(f"Connection closed for {channel}: {e} (attempt {attempt + 1})")
            # For 4006 errors, wait longer before retry
            if hasattr(e, 'code') and e.code == 4006:
                logger.info("Got 4006 error, waiting extra time...")
                await asyncio.sleep(10)
        except discord.ClientException as e:
            logger.error(f"Discord client error: {e} (attempt {attempt + 1})")
            # Bot might already be connected
            if "already connected" in str(e).lower():
                existing = discord.utils.get(bot.voice_clients, guild=channel.guild)
                if existing and existing.is_connected():
                    return existing
        except Exception as e:
            logger.error(f"Unexpected error connecting to {channel}: {e} (attempt {attempt + 1})")

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

HUGGINGFACE_API_KEY = os.environ.get("HUGGINGFACE_API_KEY")

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

    # Defer the response immediately to prevent timeout
    await interaction.response.defer()

    duration = duration.lower()
    if not any(duration.endswith(suffix) for suffix in ["m", "h", "d", "w", "y"]):
        duration = 0

    payload = {
        "username": username,
        "duration": duration,
        "reason": reason,
        "mod": interaction.user.id,
        "token": os.environ.get("BAN_TOKEN")
    }

    try:
        # Use async http client with timeout
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(WEBHOOK_URL, json=payload)
            
            if response.status_code == 200:
                await interaction.followup.send(f"the dumbah {username} has been banned from sbtd 💀")
            else:
                await interaction.followup.send(f"skill issue: {response.status_code}", ephemeral=True)
                
    except httpx.TimeoutException:
        await interaction.followup.send(f"request timed out 💀💀💀", ephemeral=True)
    except httpx.ConnectError:
        await interaction.followup.send(f"connection failed 💀💀💀", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"request err: 💀💀💀 {type(e).__name__}", ephemeral=True)

# Enhanced play command with better error handling
@bot.tree.command(name="play", description="plays music from youtube or adds to queue")
@app_commands.describe(url="youtube url or search query")
async def play_command(interaction: discord.Interaction, url: str):
    await interaction.response.defer()

    # Check if user is in a voice channel
    if not interaction.user.voice:
        await interaction.followup.send("bro join a vc first 💀", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    guild = interaction.guild

    # Check for existing voice client
    voice_client = discord.utils.get(bot.voice_clients, guild=guild)

    # If not connected or connection is broken, try to connect
    if not voice_client or not voice_client.is_connected():
        # Clean up any broken connections first
        if voice_client:
            try:
                await voice_client.disconnect(force=True)
            except:
                pass
        
        voice_client = await connect_to_voice_channel(channel)
        if not voice_client:
            await interaction.followup.send(
                "l bob is dumb or gay and made bot break 💀💀💀💀", 
                ephemeral=True
            )
            return
        current_player[guild.id] = interaction.user.id

    # Enhanced yt-dlp options
    ydl_opts = {
        "format": "bestaudio[ext=webm]/bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractaudio": False,
        "audioformat": "best",
        "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
        # Better headers and options
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
        "cookiefile": None,
        "extractor_args": {
            "youtube": {
                "skip": ["dash", "hls"],
                "player_skip": ["configs", "webpage"]
            }
        }
    }

    try:
        # Search if not a URL
        if not youtube_regex.match(url):
            url = f"ytsearch:{url}"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.ExtractorError as e:
                error_msg = str(e).lower()
                if any(keyword in error_msg for keyword in ["sign in", "confirm", "bot", "blocked"]):
                    await interaction.followup.send(
                        "w the l ass song didnt play 🎖️🎖️🎖️🎖️",
                        ephemeral=True
                    )
                    return
                elif "private" in error_msg or "unavailable" in error_msg:
                    await interaction.followup.send(
                        "llll imagine putting a private video 💀",
                        ephemeral=True
                    )
                    return
                else:
                    raise e

            # Handle search results
            if 'entries' in info:
                if not info['entries']:
                    await interaction.followup.send("wha", ephemeral=True)
                    return
                info = info['entries'][0]

            if not info:
                await interaction.followup.send("skill issue", ephemeral=True)
                return

            title = info.get('title', 'Unknown')
            duration = info.get('duration', 0)
            webpage_url = info.get('webpage_url', url)

            # Create track info
            track = TrackInfo(
                url=webpage_url,
                title=title,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                duration=duration
            )

            # Add to queue
            queue = get_queue(guild.id)
            queue.append(track)

            # Start playing if nothing is playing
            if not voice_client.is_playing():
                await play_next_track(guild, voice_client)
                await interaction.followup.send(f"ight ima play: **{title}**")
            else:
                position = len(queue)
                await interaction.followup.send(f"added l song into the trash can 💀💀🗑️🗑: (#{position}): **{title}**")

    except Exception as e:
        logger.error(f"Error in play command: {e}")
        await interaction.followup.send(f"something went wrong 💀", ephemeral=True)

@bot.tree.command(name="export-ids", description="noobi asked me for this")
async def export_ids(interaction: discord.Interaction):
    """llll"""
    try:
        # Defer the response since fetching members can take time
        await interaction.response.defer(ephemeral=True)
        
        # Fetch all members from the guild (this loads them into cache)
        async for member in interaction.guild.fetch_members(limit=None):
            pass  # This populates the member cache
        
        # Now get all members from the guild
        members = interaction.guild.members
        
        # Create filename with guild name and timestamp
        guild_name = interaction.guild.name.replace(' ', '_')
        filename = f"{guild_name}_member_ids.txt"
        
        # Write member IDs to file
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(f"member IDs for {interaction.guild.name}\n")
            f.write(f"memebers: {len(members)}\n")
            f.write("=" * 50 + "\n\n")
            
            for member in members:
                # Write ID and username for easier identification
                f.write(f"{member.id} - {member.display_name}\n")
        
        # Send the file as an attachment
        file = discord.File(filename)
        await interaction.response.send_message(
            f"ok noobi heres {len(members)} ids in a txt:",
            file=file,
            ephemeral=True
        )
        
    except Exception as e:
        await interaction.response.send_message(
            f"err💀💀💀💀 {str(e)}",
            ephemeral=True
        )


# Fixed /remove command - properly handles queue management
@bot.tree.command(name="remove", description="removes ur own songs from the queue")
async def remove_command(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    
    if not queue:
        await interaction.response.send_message("theres nothing in the queue to remove 💀", ephemeral=True)
        return

    # Find user's tracks in the queue
    user_tracks = []
    for i, track in enumerate(queue):
        if track.user_id == interaction.user.id:
            user_tracks.append((i, track))

    if not user_tracks:
        await interaction.response.send_message("u dont have any songs in the queue 💀", ephemeral=True)
        return

    # If user has only one song, remove it directly
    if len(user_tracks) == 1:
        queue_pos, track = user_tracks[0]
        queue_list = list(queue)
        removed_track = queue_list.pop(queue_pos)
        queue.clear()
        queue.extend(queue_list)
        
        await interaction.response.send_message(f"removed from queue: **{removed_track.title}**", ephemeral=True)
        return

    # If user has multiple songs, show selection interface
    embed = discord.Embed(
        title="select which song to remove:",
        description="choose which of ur songs to remove from the queue:",
        color=0xff0000
    )

    for i, (queue_pos, track) in enumerate(user_tracks[:5]):  # Show max 5 songs
        embed.add_field(
            name=f"{i + 1}. {track.title}",
            value=f"position in queue: {queue_pos + 1}",
            inline=False
        )

    if len(user_tracks) > 5:
        embed.add_field(
            name="note:",
            value=f"showing first 5 of {len(user_tracks)} songs",
            inline=False
        )

    view = TrackRemovalView(interaction.user.id, queue, user_tracks)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# Fixed /skip command - enhanced permission checking and error handling
@bot.tree.command(name="skip", description="skips to the next track (requires manage messages permission or being the host)")
async def skip_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("bot is not connected to a voice channel 💀", ephemeral=True)
        return

    if not voice_client.is_playing():
        await interaction.response.send_message("nothing is playing 💀💀💀", ephemeral=True)
        return

    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Check if user can skip:
    # 1. Has manage messages permission (moderator)
    # 2. Is the host (started the music session)
    # 3. Current song is their own (can skip their own songs)
    can_skip = False
    skip_reason = ""

    if perms.manage_messages:
        can_skip = True
        skip_reason = "moderator"
    elif host_id == interaction.user.id:
        can_skip = True
        skip_reason = "host"
    elif current_track and current_track.user_id == interaction.user.id:
        can_skip = True
        skip_reason = "own song"

    if not can_skip:
        await interaction.response.send_message(
            "u cant skip this track 💀 (need manage messages permission, be the host, or skip ur own song)",
            ephemeral=True
        )
        return

    track_name = current_track.title if current_track else "current track"
    
    # Stop current track (this will trigger the after_playing callback)
    voice_client.stop()
    
    # Check if there's anything in queue
    queue = get_queue(interaction.guild.id)
    if queue:
        await interaction.response.send_message(f"⏭️ skipped: **{track_name}**")
    else:
        await interaction.response.send_message(f"⏭️ skipped: **{track_name}** (queue is now empty)")


# Fixed /leave command - enhanced permission checking and cleanup
@bot.tree.command(name="leave", description="makes the bot leave the voice channel")
async def leave_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("bot is not in a voice channel 💀", ephemeral=True)
        return

    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Check if there's music playing or in queue
    has_music = current_track is not None or len(queue) > 0
    
    # Permission check - allow if:
    # 1. User has manage messages permission (moderator)
    # 2. User is the host and no one else has songs
    # 3. No music is playing/queued
    can_leave = False
    
    if perms.manage_messages:
        can_leave = True
    elif not has_music:
        can_leave = True
    elif host_id == interaction.user.id:
        # Host can leave if only their songs remain
        only_host_songs = True
        
        # Check current track
        if current_track and current_track.user_id != host_id:
            only_host_songs = False
        
        # Check queue
        if only_host_songs:
            for track in queue:
                if track.user_id != host_id:
                    only_host_songs = False
                    break
        
        if only_host_songs:
            can_leave = True

    if not can_leave:
        await interaction.response.send_message(
            "cant make the bot leave while music is playing 💀 (need manage messages permission or be the host with only ur songs)",
            ephemeral=True
        )
        return

    # Enhanced cleanup
    try:
        await cleanup_voice_client(interaction.guild.id)
        await interaction.response.send_message("ight this is a l vc only noobi is here im leaving")
    except Exception as e:
        logger.error(f"Error during leave command: {e}")
        await interaction.response.send_message("ll it didnt work 💀💀💀💀", ephemeral=True)


# Fixed /stop command - enhanced permission checking and queue management
@bot.tree.command(name="stop", description="stops the current audio and clears the queue")
async def stop_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("bot is not in a voice channel 💀", ephemeral=True)
        return

    if not voice_client.is_playing():
        await interaction.response.send_message("nothing is playing 💀", ephemeral=True)
        return

    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Permission check - allow if:
    # 1. User has manage messages permission (moderator)
    # 2. User is the host
    # 3. Only user's own songs are playing/queued
    can_stop = False
    
    if perms.manage_messages:
        can_stop = True
    elif host_id == interaction.user.id:
        can_stop = True
    else:
        # Check if only user's songs are in queue/playing
        only_user_songs = True
        
        # Check current track
        if current_track and current_track.user_id != interaction.user.id:
            only_user_songs = False
        
        # Check queue
        if only_user_songs:
            for track in queue:
                if track.user_id != interaction.user.id:
                    only_user_songs = False
                    break
        
        if only_user_songs:
            can_stop = True

    if not can_stop:
        await interaction.response.send_message(
            "cant stop the music 💀 (need manage messages permission, be the host, or only have ur own songs)",
            ephemeral=True
        )
        return

    # Count items being cleared
    queue_count = len(queue)
    current_song = current_track.title if current_track else "unknown"
    
    # Clear the queue and stop playback
    queue.clear()
    currently_playing.pop(interaction.guild.id, None)
    
    # Stop the current track
    voice_client.stop()
    
    if queue_count > 0:
        await interaction.response.send_message(
            f"ok ima stop"
        )
    else:
        await interaction.response.send_message(f"ok i stooped the trash ah song: **{current_song}**")


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

if token:
    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
else:
    logger.error("ERROR: DISCORD_TOKEN not found in environment variables.")

serverig.keep_alive()

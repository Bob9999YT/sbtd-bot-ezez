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
    import naclfdes
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

async def connect_to_voice_channel(channel, max_retries=3):
    """
    Robust voice channel connection with retry logic
    """
    for attempt in range(max_retries):
        try:
            # Clean up any existing connections first
            guild = channel.guild
            voice_client = discord.utils.get(guild.voice_clients)
            
            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                    await asyncio.sleep(1)  # Wait for cleanup
                except:
                    pass
            
            # Create new connection with timeout
            voice_client = await asyncio.wait_for(
                channel.connect(timeout=30.0, reconnect=True),
                timeout=45.0
            )
            
            # Verify connection is stable
            if voice_client and voice_client.is_connected():
                return voice_client
            else:
                raise Exception("Connection established but not stable")
                
        except asyncio.TimeoutError:
            print(f"Voice connection timeout (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                continue
                
        except discord.errors.ConnectionClosed as e:
            print(f"Voice connection closed with code {e.code} (attempt {attempt + 1}/{max_retries})")
            if e.code == 4006:  # Session timeout
                if attempt < max_retries - 1:
                    await asyncio.sleep(3)  # Wait longer for session timeout
                    continue
            elif e.code == 4014:  # Disconnected
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                    
        except Exception as e:
            print(f"Voice connection error: {e} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
    
    return None

# Enhanced voice client management
class VoiceManager:
    def __init__(self):
        self.voice_clients = {}
        self.reconnect_tasks = {}
    
    async def get_voice_client(self, guild_id):
        """Get or create voice client for guild"""
        if guild_id in self.voice_clients:
            vc = self.voice_clients[guild_id]
            if vc and vc.is_connected():
                return vc
            else:
                # Clean up stale connection
                del self.voice_clients[guild_id]
        return None
    
    async def create_voice_client(self, channel):
        """Create new voice client with proper error handling"""
        guild_id = channel.guild.id
        
        # Cancel any existing reconnect tasks
        if guild_id in self.reconnect_tasks:
            self.reconnect_tasks[guild_id].cancel()
            del self.reconnect_tasks[guild_id]
        
        # Create new connection
        voice_client = await connect_to_voice_channel(channel)
        
        if voice_client:
            self.voice_clients[guild_id] = voice_client
            
            # Set up disconnect handler
            async def on_disconnect():
                if guild_id in self.voice_clients:
                    del self.voice_clients[guild_id]
            
            voice_client.on_disconnect = on_disconnect
            
        return voice_client
    
    async def disconnect_all(self):
        """Disconnect all voice clients"""
        for guild_id, vc in list(self.voice_clients.items()):
            try:
                await vc.disconnect(force=True)
            except:
                pass
        self.voice_clients.clear()

# Initialize voice manager
voice_manager = VoiceManager()

# Improved FFmpeg options for Render
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 200M',
    'options': '-vn -bufsize 512k'
}


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

# Better audio source creation
def create_audio_source(url):
    """Create audio source with better error handling"""
    try:
        return discord.FFmpegPCMAudio(
            url,
            **FFMPEG_OPTIONS,
            executable='ffmpeg'  # Explicitly specify ffmpeg path
        )
    except Exception as e:
        logger.error(f"Failed to create audio source: {e}")
        return None
    
async def play_next_track(guild, voice_client):
    """Play next track with better error handling"""
    queue = get_queue(guild.id)
    
    if not queue:
        logger.info(f"Queue empty for guild {guild.id}")
        return
    
    track = queue.pop(0)
    logger.info(f"Playing: {track.title}")
    
    try:
        # Get fresh URL with shorter timeout
        ydl_opts = {
            "format": "bestaudio[ext=webm]/bestaudio/best",
            "quiet": True,
            "socket_timeout": 30,
            "retries": 2,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track.url, download=False)
            url = info['url']
        
        # Create audio source
        audio_source = create_audio_source(url)
        if not audio_source:
            logger.error("Failed to create audio source, skipping track")
            await play_next_track(guild, voice_client)
            return
        
        # Play with error handling
        def after_playing(error):
            if error:
                logger.error(f"Player error: {error}")
            else:
                logger.info("Track finished playing")
            
            # Schedule next track
            asyncio.run_coroutine_threadsafe(
                play_next_track(guild, voice_client),
                bot.loop
            )
        
        voice_client.play(audio_source, after=after_playing)
        
    except Exception as e:
        logger.error(f"Error playing track: {e}")
        # Try to play next track
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
                            f"idk what to even say at this point {response.status_code}),
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

async def ban_command(interaction: discord.Interaction, username: str, duration: str, reason: str):
    print("COMMAND STARTED")
    try:
        print("Deferring interaction response...")
        await interaction.response.defer(ephemeral=True)
        print("Deferred successfully ✅")
    except Exception as e:
        print("❌ Defer failed:", type(e).__name__, "-", e)
        return

    if interaction.user.id not in ALLOWED_USER_IDS:
        print(f"Unauthorized user: {interaction.user.id}")
        await interaction.followup.send("bros not a mod 💀💀", ephemeral=True)
        return

    duration = duration.lower()
    if not any(duration.endswith(suffix) for suffix in ["m", "h", "d", "w", "y"]):
        print(f"Invalid duration format: {duration}")
        duration = "0"

    payload = {
        "username": username,
        "duration": duration,
        "reason": reason,
        "mod": interaction.user.id,
        "token": os.environ.get("BAN_TOKEN")
    }

    print("Payload prepared:", payload)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot/1.0"
    }

    try:
        print("Sending POST request to Google Script...")
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            response = await client.post(WEBHOOK_URL, json=payload, headers=headers)

        print("Response status code:", response.status_code)
        print("Redirect location header:", response.headers.get("location"))
        print("Response text:", response.text)

        if response.status_code == 200:
            await interaction.followup.send(f"the dumbah {username} has been banned from sbtd 💀")
        elif response.status_code == 302:
            await interaction.followup.send("skill issue: 302 🔁 redirected", ephemeral=True)
        else:
            await interaction.followup.send(f"skill issue: {response.status_code}", ephemeral=True)

    except httpx.HTTPError as e:
        print("HTTPError:", e)
        await interaction.followup.send(f"request died 💀 {type(e).__name__}", ephemeral=True)
    except Exception as e:
        print("Unexpected error:", type(e).__name__, "-", e)
        await interaction.followup.send(f"request err: 💀 {type(e).__name__}", ephemeral=True)

# Updated play command with better error handling
@bot.tree.command(name="play", description="plays music from youtube or adds to queue")
@app_commands.describe(url="youtube url or search query")
async def play_command(interaction: discord.Interaction, url: str):
    try:
        await interaction.response.defer()
    except discord.NotFound:
        await interaction.followup.send("loading 💀💀💀💀", ephemeral=True)
        return
    except discord.HTTPException:
        return

    # Check if user is in a voice channel
    if not interaction.user.voice:
        try:
            await interaction.followup.send("bro join a vc first 💀", ephemeral=True)
        except:
            pass
        return

    channel = interaction.user.voice.channel
    guild = interaction.guild

    # Check bot permissions
    permissions = channel.permissions_for(guild.me)
    if not permissions.connect or not permissions.speak:
        try:
            await interaction.followup.send("i dont have perms to join/speak in that vc 💀", ephemeral=True)
        except:
            pass
        return

    # Get or create voice client
    voice_client = await voice_manager.get_voice_client(guild.id)
    
    if not voice_client:
        try:
            await interaction.followup.send("connecting to voice channel...", ephemeral=True)
        except:
            pass
            
        voice_client = await voice_manager.create_voice_client(channel)
        
        if not voice_client:
            try:
                await interaction.followup.send(
                    "couldn't connect to voice channel, try again later 💀", 
                    ephemeral=True
                )
            except:
                pass
            return
    
    # Move to the user's channel if needed
    if voice_client.channel != channel:
        try:
            await voice_client.move_to(channel)
        except Exception as e:
            print(f"Failed to move to channel: {e}")
            # Try to reconnect
            voice_client = await voice_manager.create_voice_client(channel)
            if not voice_client:
                try:
                    await interaction.followup.send(
                        "couldn't move to your voice channel 💀", 
                        ephemeral=True
                    )
                except:
                    pass
                return


# Additional helper function for Render deployment
async def ensure_ffmpeg_available():
    """Ensure FFmpeg is available on Render"""
    try:
        # Check if ffmpeg is available
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except:
        return False


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

# Enhanced /stop command with better error handling and voice client detection
@bot.tree.command(name="stop", description="stops the current audio and clears the queue")
async def stop_command(interaction: discord.Interaction):
    try:
        # Defer response to avoid timeout
        await interaction.response.defer()
    except discord.NotFound:
        return
    except discord.HTTPException:
        return
    
    # Enhanced voice client detection with fallbacks
    voice_client = None
    
    # Method 1: Standard discord.py method
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    
    # Method 2: Check voice manager if available
    if not voice_client and 'voice_manager' in globals():
        voice_client = await voice_manager.get_voice_client(interaction.guild.id)
    
    # Method 3: Manual search
    if not voice_client:
        for vc in bot.voice_clients:
            if vc.guild.id == interaction.guild.id:
                voice_client = vc
                break
    
    if not voice_client or not voice_client.is_connected():
        try:
            await interaction.followup.send("bot is not in a voice channel 💀", ephemeral=True)
        except:
            pass
        return
    
    # Check if actually playing (with error handling)
    is_playing = False
    try:
        is_playing = voice_client.is_playing()
    except Exception as e:
        logger.error(f"Error checking if playing: {e}")
        # Try to get state from our tracking
        current_track = currently_playing.get(interaction.guild.id)
        is_playing = current_track is not None
    
    if not is_playing:
        try:
            await interaction.followup.send("nothing is playing 💀", ephemeral=True)
        except:
            pass
        return
    
    # Get current state
    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions
    
    # Permission check - allow if:
    # 1. User has manage messages permission (moderator)
    # 2. User is the host
    # 3. Only user's own songs are playing/queued
    # 4. Bot is alone in voice channel (emergency case)
    can_stop = False
    
    # Check if bot is alone in voice channel
    bot_alone = len(voice_client.channel.members) <= 1
    
    if perms.manage_messages:
        can_stop = True
    elif host_id == interaction.user.id:
        can_stop = True
    elif bot_alone:
        can_stop = True  # Allow if bot is alone
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
        try:
            await interaction.followup.send(
                "bro tried 💀💀💀",
                ephemeral=True
            )
        except:
            pass
        return
    
    # Count items being cleared
    queue_count = len(queue)
    current_song = current_track.title if current_track else "unknown"
    
    # Clear the queue and stop playback with error handling
    try:
        queue.clear()
        currently_playing.pop(interaction.guild.id, None)
        
        # Stop the current track
        voice_client.stop()
        
        # Send success message
        if queue_count > 0:
            await interaction.followup.send(f"ok ima stop playing and cleared {queue_count} songs from queue")
        else:
            await interaction.followup.send(f"ok i stopped the trash ah song: **{current_song}**")
            
    except Exception as e:
        logger.error(f"Error stopping playback: {e}")
        try:
            await interaction.followup.send("tried to stop but something went wrong 💀", ephemeral=True)
        except:
            pass

# Enhanced /skip command with better error handling and voice client detection
@bot.tree.command(name="skip", description="skips to the next track (requires manage messages permission or being the host)")
async def skip_command(interaction: discord.Interaction):
    try:
        # Defer response to avoid timeout
        await interaction.response.defer()
    except discord.NotFound:
        return
    except discord.HTTPException:
        return
    
    # Enhanced voice client detection with fallbacks
    voice_client = None
    
    # Method 1: Standard discord.py method
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    
    # Method 2: Check voice manager if available
    if not voice_client and 'voice_manager' in globals():
        voice_client = await voice_manager.get_voice_client(interaction.guild.id)
    
    # Method 3: Manual search
    if not voice_client:
        for vc in bot.voice_clients:
            if vc.guild.id == interaction.guild.id:
                voice_client = vc
                break
    
    if not voice_client or not voice_client.is_connected():
        try:
            await interaction.followup.send("bot is not connected to a voice channel 💀", ephemeral=True)
        except:
            pass
        return
    
    # Check if actually playing (with error handling)
    is_playing = False
    try:
        is_playing = voice_client.is_playing()
    except Exception as e:
        logger.error(f"Error checking if playing: {e}")
        # Try to get state from our tracking
        current_track = currently_playing.get(interaction.guild.id)
        is_playing = current_track is not None
    
    if not is_playing:
        try:
            await interaction.followup.send("nothing is playing 💀💀💀", ephemeral=True)
        except:
            pass
        return
    
    # Get current state
    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions
    
    # Check if user can skip:
    # 1. Has manage messages permission (moderator)
    # 2. Is the host (started the music session)
    # 3. Current song is their own (can skip their own songs)
    # 4. Bot is alone in voice channel (emergency case)
    can_skip = False
    skip_reason = ""
    
    # Check if bot is alone in voice channel
    bot_alone = len(voice_client.channel.members) <= 1
    
    if perms.manage_messages:
        can_skip = True
        skip_reason = "moderator"
    elif host_id == interaction.user.id:
        can_skip = True
        skip_reason = "host"
    elif current_track and current_track.user_id == interaction.user.id:
        can_skip = True
        skip_reason = "own song"
    elif bot_alone:
        can_skip = True
        skip_reason = "bot alone"
    
    if not can_skip:
        try:
            await interaction.followup.send(
                "l 💀💀💀",
                ephemeral=True
            )
        except:
            pass
        return
    
    track_name = current_track.title if current_track else "current track"
    
    # Stop current track (this will trigger the after_playing callback)
    try:
        voice_client.stop()
        
        # Check if there's anything in queue
        queue = get_queue(interaction.guild.id)
        if queue:
            await interaction.followup.send(f"ok good you skipped the trash ah song **{track_name}** 💀💀")
        else:
            await interaction.followup.send(f"ok good you skipped the trash ah song **{track_name}** the trash bin is now empty great job ✅✅✅")
            
    except Exception as e:
        logger.error(f"Error skipping track: {e}")
        try:
            await interaction.followup.send("lll bro failed 💀💀💀", ephemeral=True)
        except:
            pass

# Enhanced /pause command
@bot.tree.command(name="pause", description="pauses the current audio")
async def pause_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except:
        return
    
    # Enhanced voice client detection
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    
    if not voice_client or not voice_client.is_connected():
        try:
            await interaction.followup.send("bro is not in 💀💀", ephemeral=True)
        except:
            pass
        return
    
    try:
        if voice_client.is_playing():
            voice_client.pause()
            await interaction.followup.send("paused the garbage into some bozo plays it again 💀💀")
        elif voice_client.is_paused():
            await interaction.followup.send("its already paused u noob 💀💀💀", ephemeral=True)
        else:
            await interaction.followup.send("nothing is playing 💀", ephemeral=True)
    except Exception as e:
        logger.error(f"Error pausing: {e}")
        try:
            await interaction.followup.send("💀", ephemeral=True)
        except:
            pass

# Enhanced /resume command
@bot.tree.command(name="resume", description="resumes the paused audio")
async def resume_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except:
        return
    
    # Enhanced voice client detection
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    
    if not voice_client or not voice_client.is_connected():
        try:
            await interaction.followup.send("ll bro is not in 💀💀", ephemeral=True)
        except:
            pass
        return
    
    try:
        if voice_client.is_paused():
            voice_client.resume()
            await interaction.followup.send("resumed the trash think fast chucklenuts")
        elif voice_client.is_playing():
            await interaction.followup.send("l its alr playing 💀💀", ephemeral=True)
        else:
            await interaction.followup.send("idk what to say for it ur just stupid 💀💀💀", ephemeral=True)
    except Exception as e:
        logger.error(f"Error resuming: {e}")
        try:
            await interaction.followup.send("💀💀💀💀✅✅🗑️🗑️💀💀", ephemeral=True)
        except:
            pass


@bot.tree.command(name="leave", description="makes the bot leave the voice channel")
async def leave_command(interaction: discord.Interaction):
    try:
        # Defer response to avoid timeout
        await interaction.response.defer()
    except discord.NotFound:
        # Interaction expired
        return
    except discord.HTTPException:
        # Discord API error
        return
    
    # Get voice client with multiple fallback methods
    voice_client = None
    
    # Method 1: Standard discord.py method
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    
    # Method 2: Check if using voice manager (from previous fix)
    if not voice_client and 'voice_manager' in globals():
        voice_client = await voice_manager.get_voice_client(interaction.guild.id)
    
    # Method 3: Manual search through all voice clients
    if not voice_client:
        for vc in bot.voice_clients:
            if vc.guild.id == interaction.guild.id:
                voice_client = vc
                break
    
    # Check if bot is actually connected
    if not voice_client or not voice_client.is_connected():
        try:
            await interaction.followup.send("bot is not in a voice channel 💀", ephemeral=True)
        except:
            pass
        return
    
    # Get current state
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
    # 4. Bot is alone in voice channel (emergency case)
    can_leave = False
    
    # Check if bot is alone in voice channel
    bot_alone = len(voice_client.channel.members) <= 1
    
    if perms.manage_messages:
        can_leave = True
    elif not has_music:
        can_leave = True
    elif bot_alone:
        can_leave = True  # Allow leave if bot is alone
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
        try:
            await interaction.followup.send(
                "ll bro tried 💀💀💀",
                ephemeral=True
            )
        except:
            pass
        return
    
    # Enhanced cleanup with multiple attempts
    cleanup_success = False
    
    try:
        # Method 1: Use existing cleanup function
        if 'cleanup_voice_client' in globals():
            await cleanup_voice_client(interaction.guild.id)
            cleanup_success = True
        else:
            # Method 2: Manual cleanup
            await manual_cleanup(interaction.guild.id, voice_client)
            cleanup_success = True
            
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        
        # Method 3: Force disconnect as fallback
        try:
            await voice_client.disconnect(force=True)
            await manual_cleanup(interaction.guild.id, voice_client)
            cleanup_success = True
        except Exception as e2:
            logger.error(f"Force disconnect also failed: {e2}")
    
    # Send response based on cleanup success
    try:
        if cleanup_success:
            await interaction.followup.send("ight this is a l vc only noobi is here im leaving 💀")
        else:
            await interaction.followup.send("tried to leave but something went wrong 💀", ephemeral=True)
    except:
        # If we can't send a response, at least log it
        logger.error("Failed to send leave command response")

# Manual cleanup function for when cleanup_voice_client doesn't exist
async def manual_cleanup(guild_id, voice_client):
    """Manual cleanup when main cleanup function isn't available"""
    try:
        # Stop any playing audio
        if voice_client.is_playing():
            voice_client.stop()
        
        # Clear queue and state
        if guild_id in queues:
            queues[guild_id].clear()
        
        if guild_id in currently_playing:
            del currently_playing[guild_id]
        
        if guild_id in current_player:
            del current_player[guild_id]
        
        # Clear voice manager state if it exists
        if 'voice_manager' in globals() and guild_id in voice_manager.voice_clients:
            del voice_manager.voice_clients[guild_id]
        
        # Disconnect
        await voice_client.disconnect(force=True)
        
    except Exception as e:
        logger.error(f"Manual cleanup error: {e}")
        raise

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

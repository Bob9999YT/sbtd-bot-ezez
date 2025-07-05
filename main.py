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

class TrackRemovalView(discord.ui.View):
    def __init__(self, user_id, queue, user_tracks):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.queue = queue
        self.user_tracks = user_tracks
        
        # Add buttons for each track (max 5)
        for i, (queue_pos, track) in enumerate(user_tracks[:5]):
            button = discord.ui.Button(
                label=f"Remove #{i+1}",
                style=discord.ButtonStyle.red,
                custom_id=f"remove_{i}"
            )
            button.callback = self.create_callback(i)
            self.add_item(button)
    
    def create_callback(self, index):
        async def callback(interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This isn't your removal menu!", ephemeral=True)
                return
            
            queue_pos, track = self.user_tracks[index]
            queue_list = list(self.queue)
            
            # Find and remove the track
            for i, q_track in enumerate(queue_list):
                if q_track.url == track.url and q_track.user_id == track.user_id:
                    removed_track = queue_list.pop(i)
                    break
            
            # Update the queue
            self.queue.clear()
            self.queue.extend(queue_list)
            
            await interaction.response.send_message(f"Removed: **{track.title}**", ephemeral=True)
            self.stop()
        
        return callback

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

# FIXED: Enhanced cleanup function with better state management
async def cleanup_voice_client(guild_id):
    """Enhanced cleanup with better error handling and state management"""
    try:
        logger.info(f"Starting cleanup for guild {guild_id}")
        
        # Get voice client
        voice_client = discord.utils.get(bot.voice_clients, guild=bot.get_guild(guild_id))
        
        if voice_client:
            try:
                # Stop playback first
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
                    await asyncio.sleep(0.5)  # Give time for stop to process
                
                # Disconnect with force
                if voice_client.is_connected():
                    await voice_client.disconnect(force=True)
                    logger.info(f"Disconnected voice client for guild {guild_id}")
                    
            except Exception as e:
                logger.error(f"Error disconnecting voice client for guild {guild_id}: {e}")
                # Force cleanup even if disconnect fails
                try:
                    await voice_client.cleanup()
                except:
                    pass

        # Clean up all state data
        current_player.pop(guild_id, None)
        currently_playing.pop(guild_id, None)
        connection_retries.pop(guild_id, None)
        voice_connections.pop(guild_id, None)
        
        if guild_id in music_queues:
            music_queues[guild_id].clear()
            
        logger.info(f"Completed cleanup for guild {guild_id}")

    except Exception as e:
        logger.error(f"Error in cleanup_voice_client for guild {guild_id}: {e}")

# FIXED: Improved voice connection with proper retry logic and state tracking
async def connect_to_voice_channel(channel, max_retries=2):
    """Robust voice channel connection with retry logic and state tracking"""
    guild_id = channel.guild.id
    
    # Clean up any existing connection first
    existing_vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
    if existing_vc:
        try:
            await existing_vc.disconnect(force=True)
            await asyncio.sleep(1)  # Wait for cleanup
        except:
            pass
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Attempting voice connection to {channel.name} (attempt {attempt + 1}/{max_retries})")
            
            # Connect with shorter timeout to fail faster
            voice_client = await asyncio.wait_for(
                channel.connect(timeout=15.0, reconnect=False),  # Disabled reconnect to avoid loops
                timeout=20.0
            )
            
            # Verify connection is stable
            if voice_client and voice_client.is_connected():
                voice_connections[guild_id] = voice_client
                logger.info(f"Successfully connected to voice channel in guild {guild_id}")
                return voice_client
            else:
                logger.warning(f"Connection established but not stable for guild {guild_id}")
                raise Exception("Connection established but not stable")
                
        except asyncio.TimeoutError:
            logger.error(f"Voice connection timeout for guild {guild_id} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(2)  # Short wait before retry
                continue
                
        except Exception as e:
            logger.error(f"Voice connection error for guild {guild_id}: {e} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(2)  # Short wait before retry
                continue
    
    logger.error(f"Failed to connect to voice channel after {max_retries} attempts")
    return None

# FIXED: Better FFmpeg options to prevent 4008 errors
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M -analyzeduration 0',
    'options': '-vn -bufsize 256k -ac 2 -ar 48000 -f s16le'
}

def create_audio_source(url):
    """Create audio source with better error handling"""
    try:
        return discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
    except Exception as e:
        logger.error(f"Failed to create audio source: {e}")
        return None

# FIXED: Improved play_next_track function with better error handling and 4008 prevention
async def play_next_track(guild, voice_client):
    """Play next track with better error handling and 4008 prevention"""
    try:
        guild_id = guild.id
        
        # Verify voice client is still valid and connected
        if not voice_client or not voice_client.is_connected():
            logger.error(f"Voice client disconnected during playback for guild {guild_id}")
            await cleanup_voice_client(guild_id)
            return
        
        # Double-check we're still in the voice connections tracker
        if guild_id not in voice_connections or voice_connections[guild_id] != voice_client:
            logger.error(f"Voice client mismatch for guild {guild_id}")
            await cleanup_voice_client(guild_id)
            return
        
        queue = get_queue(guild_id)
        
        if not queue:
            currently_playing.pop(guild_id, None)
            logger.info(f"Queue empty for guild {guild_id}")
            return
        
        track = queue.popleft()
        currently_playing[guild_id] = track
        logger.info(f"Playing: {track.title} in guild {guild_id}")
        
        # Get fresh URL with better error handling and faster extraction
        ydl_opts = {
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 15,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "prefer_ffmpeg": True,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, lambda: ydl.extract_info(track.url, download=False)
                    ),
                    timeout=30.0  # Reduced timeout
                )
                
                if 'url' in info:
                    url = info['url']
                elif 'formats' in info and info['formats']:
                    # Find best audio format
                    audio_formats = [f for f in info['formats'] if f.get('vcodec') == 'none']
                    if audio_formats:
                        url = audio_formats[0]['url']
                    else:
                        url = info['formats'][0]['url']
                else:
                    raise Exception("No playable URL found")
                    
        except Exception as e:
            logger.error(f"Error extracting URL for guild {guild_id}: {e}")
            # Try next track instead of failing completely
            await asyncio.sleep(1)
            await play_next_track(guild, voice_client)
            return
        
        # Create audio source with optimized settings
        try:
            audio_source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        except Exception as e:
            logger.error(f"Failed to create audio source for guild {guild_id}: {e}")
            await asyncio.sleep(1)
            await play_next_track(guild, voice_client)
            return
        
        # Play with enhanced error handling
        def after_playing(error):
            if error:
                logger.error(f"Player error for guild {guild_id}: {error}")
                # Don't clear currently_playing here, let the next track function handle it
            
            # Schedule next track with better error handling
            try:
                future = asyncio.run_coroutine_threadsafe(
                    play_next_track(guild, voice_client),
                    bot.loop
                )
                # Don't wait for completion to avoid blocking
            except Exception as e:
                logger.error(f"Error scheduling next track for guild {guild_id}: {e}")
                # Clean up on scheduling failure
                asyncio.run_coroutine_threadsafe(
                    cleanup_voice_client(guild_id),
                    bot.loop
                )
        
        # Final connection check before playing
        if voice_client.is_connected() and guild_id in voice_connections:
            try:
                voice_client.play(audio_source, after=after_playing)
                logger.info(f"Started playing audio for guild {guild_id}")
            except Exception as e:
                logger.error(f"Error starting playback for guild {guild_id}: {e}")
                await cleanup_voice_client(guild_id)
        else:
            logger.error(f"Voice client disconnected before starting playback for guild {guild_id}")
            await cleanup_voice_client(guild_id)
        
    except Exception as e:
        logger.error(f"Error in play_next_track for guild {guild.id}: {e}")
        currently_playing.pop(guild.id, None)
        # Try to continue with next track if possible
        try:
            queue = get_queue(guild.id)
            if queue and voice_client and voice_client.is_connected():
                await asyncio.sleep(2)  # Wait before retry
                await play_next_track(guild, voice_client)
            else:
                await cleanup_voice_client(guild.id)
        except Exception as cleanup_error:
            logger.error(f"Error during cleanup after play_next_track failure: {cleanup_error}")
            await cleanup_voice_client(guild.id)

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes and cleanup"""
    if member.bot:
        return

    # Handle user leaving voice channel
    if before.channel and not after.channel:
        voice_client = discord.utils.get(bot.voice_clients, guild=before.channel.guild)
        if voice_client and voice_client.channel == before.channel:
            # Check if we should disconnect
            remaining_members = [m for m in voice_client.channel.members if not m.bot]
            if len(remaining_members) == 0:
                logger.info(f"All users left voice channel in guild {before.channel.guild.id}")
                await asyncio.sleep(3)  # Grace period
                # Double-check
                remaining_members = [m for m in voice_client.channel.members if not m.bot]
                if len(remaining_members) == 0:
                    await cleanup_voice_client(before.channel.guild.id)

# FIXED: Enhanced play command with better connection handling
@bot.tree.command(name="play", description="Play music from YouTube or add to queue")
@app_commands.describe(url="YouTube URL or search query")
async def play_command(interaction: discord.Interaction, url: str):
    try:
        await interaction.response.defer()
        
        # Check if user is in voice channel
        if not interaction.user.voice:
            await interaction.followup.send("Join a voice channel first! 💀", ephemeral=True)
            return

        channel = interaction.user.voice.channel
        guild = interaction.guild
        guild_id = guild.id

        # Check permissions
        permissions = channel.permissions_for(guild.me)
        if not permissions.connect or not permissions.speak:
            await interaction.followup.send("I don't have permission to join/speak in that channel! 💀", ephemeral=True)
            return

        # Get or create voice client with enhanced error handling
        voice_client = discord.utils.get(bot.voice_clients, guild=guild)
        
        # Check if we need to connect or reconnect
        need_connect = (
            not voice_client or 
            not voice_client.is_connected() or 
            voice_client.channel != channel or
            guild_id not in voice_connections or
            voice_connections[guild_id] != voice_client
        )
        
        if need_connect:
            # Clean up any existing connection
            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                    await asyncio.sleep(1)
                except:
                    pass
            
            # Connect to voice channel
            voice_client = await connect_to_voice_channel(channel)
            
            if not voice_client:
                await interaction.followup.send("Failed to connect to voice channel! Try again. 💀", ephemeral=True)
                return

        # Extract track info with optimized settings
        ydl_opts = {
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "socket_timeout": 15,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Handle search queries
                if not url.startswith(('http://', 'https://')):
                    url = f"ytsearch:{url}"
                
                # Extract info with timeout
                info = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, lambda: ydl.extract_info(url, download=False)
                    ),
                    timeout=30.0
                )
                
                if 'entries' in info:
                    if not info['entries']:
                        await interaction.followup.send("No results found! 💀", ephemeral=True)
                        return
                    info = info['entries'][0]
                
                # Validate required fields
                if not info.get('title'):
                    await interaction.followup.send("Invalid video data! 💀", ephemeral=True)
                    return
                
                track = TrackInfo(
                    url=info.get('webpage_url', url),
                    title=info.get('title', 'Unknown'),
                    user_id=interaction.user.id,
                    user_name=interaction.user.display_name,
                    duration=info.get('duration')
                )
                
        except asyncio.TimeoutError:
            await interaction.followup.send("Video processing timeout! Try a different video. 💀", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Error extracting track info: {e}")
            await interaction.followup.send("Error processing that URL! Try a different one. 💀", ephemeral=True)
            return

        # Add to queue or play immediately
        queue = get_queue(guild_id)
        current_track = currently_playing.get(guild_id)
        
        if current_track or voice_client.is_playing():
            queue.append(track)
            duration_str = format_duration(track.duration)
            queue_position = len(queue)
            await interaction.followup.send(
                f"Added to queue: **{track.title}** [{duration_str}]\n"
                f"Queue position: {queue_position}"
            )
        else:
            # Set as current player if first song
            if guild_id not in current_player:
                current_player[guild_id] = interaction.user.id
            
            currently_playing[guild_id] = track
            await interaction.followup.send(f"Now playing: **{track.title}**")
            
            # Start playing
            try:
                await play_next_track(guild, voice_client)
            except Exception as e:
                logger.error(f"Error starting playback: {e}")
                await interaction.followup.send("Error starting playback! 💀", ephemeral=True)
                return

    except Exception as e:
        logger.error(f"Error in play command: {e}")
        try:
            await interaction.followup.send("Something went wrong! 💀", ephemeral=True)
        except:
            pass


# Additional commands (skip, stop, pause, resume, leave, remove) remain the same but with better error handling
@bot.tree.command(name="skip", description="Skip to the next track")
async def skip_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        
        if not voice_client or not voice_client.is_connected():
            await interaction.followup.send("Bot is not connected! 💀", ephemeral=True)
            return
        
        if not voice_client.is_playing():
            await interaction.followup.send("Nothing is playing! 💀", ephemeral=True)
            return
        
        current_track = currently_playing.get(interaction.guild.id)
        host_id = current_player.get(interaction.guild.id)
        perms = interaction.user.guild_permissions
        
        # Check permissions
        can_skip = (
            perms.manage_messages or
            host_id == interaction.user.id or
            (current_track and current_track.user_id == interaction.user.id) or
            len(voice_client.channel.members) <= 1
        )
        
        if not can_skip:
            await interaction.followup.send("You can't skip this track! 💀", ephemeral=True)
            return
        
        track_name = current_track.title if current_track else "current track"
        voice_client.stop()
        
        queue = get_queue(interaction.guild.id)
        if queue:
            await interaction.followup.send(f"Skipped: **{track_name}**")
        else:
            await interaction.followup.send(f"Skipped: **{track_name}** - Queue is now empty")
            
    except Exception as e:
        logger.error(f"Error in skip command: {e}")
        await interaction.followup.send("Error skipping track! 💀", ephemeral=True)

@bot.tree.command(name="leave", description="Make the bot leave the voice channel")
async def leave_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        
        if not voice_client or not voice_client.is_connected():
            await interaction.followup.send("Bot is not in a voice channel! 💀", ephemeral=True)
            return
        
        # Permission check
        perms = interaction.user.guild_permissions
        host_id = current_player.get(interaction.guild.id)
        
        can_leave = (
            perms.manage_messages or
            host_id == interaction.user.id or
            len(voice_client.channel.members) <= 1
        )
        
        if not can_leave:
            await interaction.followup.send("You can't make me leave! 💀", ephemeral=True)
            return
        
        await cleanup_voice_client(interaction.guild.id)
        await interaction.followup.send("Left the voice channel")
        
    except Exception as e:
        logger.error(f"Error in leave command: {e}")
        await interaction.followup.send("Error leaving channel! 💀", ephemeral=True)

@bot.tree.command(name="stop", description="Stop playback and clear queue")
async def stop_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        
        if not voice_client or not voice_client.is_connected():
            await interaction.followup.send("Bot is not connected! 💀", ephemeral=True)
            return
        
        if not voice_client.is_playing():
            await interaction.followup.send("Nothing is playing! 💀", ephemeral=True)
            return
        
        # Permission check
        perms = interaction.user.guild_permissions
        host_id = current_player.get(interaction.guild.id)
        
        can_stop = (
            perms.manage_messages or
            host_id == interaction.user.id or
            len(voice_client.channel.members) <= 1
        )
        
        if not can_stop:
            await interaction.followup.send("You can't stop the music! 💀", ephemeral=True)
            return
        
        queue = get_queue(interaction.guild.id)
        queue_count = len(queue)
        
        queue.clear()
        currently_playing.pop(interaction.guild.id, None)
        voice_client.stop()
        
        if queue_count > 0:
            await interaction.followup.send(f"Stopped playback and cleared {queue_count} songs from queue")
        else:
            await interaction.followup.send("Stopped playback")
            
    except Exception as e:
        logger.error(f"Error in stop command: {e}")
        await interaction.followup.send("Error stopping playback! 💀", ephemeral=True)

@bot.tree.command(name="pause", description="Pause the current audio")
async def pause_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        
        if not voice_client or not voice_client.is_connected():
            await interaction.followup.send("Bot is not connected! 💀", ephemeral=True)
            return
        
        if voice_client.is_playing():
            voice_client.pause()
            await interaction.followup.send("Paused playback")
        elif voice_client.is_paused():
            await interaction.followup.send("Already paused! 💀", ephemeral=True)
        else:
            await interaction.followup.send("Nothing is playing! 💀", ephemeral=True)
            
    except Exception as e:
        logger.error(f"Error in pause command: {e}")
        await interaction.followup.send("Error pausing! 💀", ephemeral=True)

@bot.tree.command(name="resume", description="Resume paused audio")
async def resume_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        
        if not voice_client or not voice_client.is_connected():
            await interaction.followup.send("Bot is not connected! 💀", ephemeral=True)
            return
        
        if voice_client.is_paused():
            voice_client.resume()
            await interaction.followup.send("Resumed playback")
        elif voice_client.is_playing():
            await interaction.followup.send("Already playing! 💀", ephemeral=True)
        else:
            await interaction.followup.send("Nothing to resume! 💀", ephemeral=True)
            
    except Exception as e:
        logger.error(f"Error in resume command: {e}")
        await interaction.followup.send("Error resuming! 💀", ephemeral=True)

@bot.tree.command(name="remove", description="Remove your songs from the queue")
async def remove_command(interaction: discord.Interaction):
    try:
        queue = get_queue(interaction.guild.id)
        
        if not queue:
            await interaction.response.send_message("Queue is empty! 💀", ephemeral=True)
            return

        # Find user's tracks
        user_tracks = []
        for i, track in enumerate(queue):
            if track.user_id == interaction.user.id:
                user_tracks.append((i, track))

        if not user_tracks:
            await interaction.response.send_message("You don't have any songs in the queue! 💀", ephemeral=True)
            return

        # If only one song, remove it directly
        if len(user_tracks) == 1:
            queue_pos, track = user_tracks[0]
            queue_list = list(queue)
            removed_track = queue_list.pop(queue_pos)
            queue.clear()
            queue.extend(queue_list)
            
            await interaction.response.send_message(f"Removed: **{removed_track.title}**", ephemeral=True)
            return

        # Multiple songs - show selection
        embed = discord.Embed(
            title="Select song to remove:",
            description="Choose which song to remove from the queue:",
            color=0xff0000
        )

        for i, (queue_pos, track) in enumerate(user_tracks[:5]):
            embed.add_field(
                name=f"{i + 1}. {track.title}",
                value=f"Queue position: {queue_pos + 1}",
                inline=False
            )

        view = TrackRemovalView(interaction.user.id, queue, user_tracks)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in remove command: {e}")
        await interaction.response.send_message("Error removing track! 💀", ephemeral=True)




@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}!")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

# FIXED: Ban command with proper redirect handling
@bot.tree.command(name="ban", description="Ban a user from SBTD")
@app_commands.describe(
    username="Username to ban",
    duration="Duration (e.g., 1h, 1d, 1w)",
    reason="Reason for ban"
)
async def ban_command(interaction: discord.Interaction, username: str, duration: str, reason: str):
    logger.info("Ban command started")
    
    try:
        await interaction.response.defer(ephemeral=True)
        logger.info("Ban command deferred successfully")
    except Exception as e:
        logger.error(f"Failed to defer ban command: {e}")
        return

    # Check permissions
    if interaction.user.id not in ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized ban attempt by user {interaction.user.id}")
        await interaction.followup.send("bros not a mod 💀💀", ephemeral=True)
        return

    # Validate duration format
    duration = duration.lower()
    if not any(duration.endswith(suffix) for suffix in ["m", "h", "d", "w", "y"]):
        logger.warning(f"Invalid duration format: {duration}")
        await interaction.followup.send("Invalid duration format! Use: 1m, 1h, 1d, 1w, 1y", ephemeral=True)
        return

    # Prepare payload
    payload = {
        "username": username,
        "duration": duration,
        "reason": reason,
        "mod": interaction.user.id,
        "token": os.environ.get("BAN_TOKEN")
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot/1.0"
    }

    try:
        logger.info("Sending ban request to webhook")
        
        # FIXED: Follow redirects and handle Google Apps Script properly
        async with httpx.AsyncClient(
            follow_redirects=True,  # Follow redirects automatically
            timeout=30.0,
            limits=httpx.Limits(max_redirects=5)
        ) as client:
            response = await client.post(WEBHOOK_URL, json=payload, headers=headers)

        logger.info(f"Ban request response - Status: {response.status_code}")
        
        if response.status_code == 200:
            try:
                # Try to parse JSON response
                response_data = response.json()
                if response_data.get("success"):
                    await interaction.followup.send(f"the dumbah {username} has been banned from sbtd 💀")
                else:
                    error_msg = response_data.get("error", "Unknown error")
                    await interaction.followup.send(f"Ban failed: {error_msg}", ephemeral=True)
            except json.JSONDecodeError:
                # If not JSON, assume success based on status code
                if "success" in response.text.lower() or "banned" in response.text.lower():
                    await interaction.followup.send(f"the dumbah {username} has been banned from sbtd 💀")
                else:
                    await interaction.followup.send(f"Ban request sent but response unclear: {response.text[:100]}", ephemeral=True)
        else:
            logger.error(f"Ban request failed with status {response.status_code}: {response.text}")
            await interaction.followup.send(f"Ban request failed: HTTP {response.status_code}", ephemeral=True)

    except httpx.TimeoutException:
        logger.error("Ban request timed out")
        await interaction.followup.send("Ban request timed out 💀", ephemeral=True)
    except httpx.HTTPError as e:
        logger.error(f"Ban request HTTP error: {e}")
        await interaction.followup.send(f"Ban request failed: {type(e).__name__}", ephemeral=True)
    except Exception as e:
        logger.error(f"Ban request unexpected error: {e}")
        await interaction.followup.send(f"Ban request error: {type(e).__name__}", ephemeral=True)

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

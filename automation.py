#!/usr/bin/env python3
"""
Automation script for batch video processing
Handles CSV/Excel parsing, video generation, subtitle generation, and YouTube uploads
"""

import sys
import json
import os
import shutil
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# Import database module
try:
    from database import check_duplicate, add_video_record, mark_video_uploaded
except ImportError:
    # Fallback if database module not found
    def check_duplicate(title, script):
        return None
    def add_video_record(*args, **kwargs):
        return True
    def mark_video_uploaded(*args, **kwargs):
        return True

# Configuration
SERVER_URL = "http://localhost:3000"
OUTPUT_DIR = "output"
TEMP_DIR = "uploads"
YOUTUBE_CREDENTIALS_FILE = "youtube_credentials.json"
FACEBOOK_CREDENTIALS_FILE = "facebook_credentials.json"
FACEBOOK_OAUTH_CONFIG_FILE = "facebook_oauth_config.json"

# Facebook: scheduled_publish_time must be 10 minutes to 30 days from now (Unix seconds)
FB_SCHEDULE_MIN_SECONDS_FROM_NOW = 600
FB_SCHEDULE_MAX_DAYS_FROM_NOW = 30

def convert_ist_to_utc(date_str, time_str):
    """Convert IST (UTC+5:30) date and time to UTC ISO format"""
    try:
        # Convert to string first to handle both pandas NaN and string 'nan'
        date_str = str(date_str).strip() if date_str is not None else ''
        time_str = str(time_str).strip() if time_str is not None else ''
        
        # Handle NaN values from pandas (check before string conversion)
        try:
            if pd.isna(date_str) or pd.isna(time_str):
                return None
        except (TypeError, ValueError):
            # If isna fails, check for 'nan' string instead
            pass
        
        # Check for 'nan' string or empty values
        if date_str.lower() == 'nan' or time_str.lower() == 'nan' or not date_str or not time_str:
            return None
        
        # Parse date and time
        date_parts = date_str.split('-')
        time_parts = time_str.split(':')
        
        if len(date_parts) != 3 or len(time_parts) < 2:
            return None
        
        year, month, day = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
        hours, minutes = int(time_parts[0]), int(time_parts[1])
        
        # Create datetime in IST (treat as naive datetime)
        dt = datetime(year, month, day, hours, minutes, 0)
        
        # IST is UTC+5:30, so subtract 5 hours 30 minutes
        utc_dt = dt - timedelta(hours=5, minutes=30)
        
        return utc_dt.isoformat() + 'Z'
    except (ValueError, TypeError, IndexError) as e:
        print(f"Error converting IST to UTC: {e} (date_str='{date_str}', time_str='{time_str}')", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error converting IST to UTC: {e}", file=sys.stderr)
        return None

def generate_video(script, watermark_settings, progress_callback=None):
    """Generate video from script"""
    try:
        if progress_callback:
            progress_callback("Generating video...")
        
        form_data = {
            'script': script
        }
        
        # Add watermark settings if enabled
        if watermark_settings.get('enable_watermark', False):
            form_data['enable_watermark'] = 'on'
            form_data['watermark_text'] = watermark_settings.get('watermark_text', '@sporky25')
            form_data['watermark_position'] = watermark_settings.get('watermark_position', 'top-middle')
            form_data['watermark_font'] = watermark_settings.get('watermark_font', 'fire-sans')
            form_data['watermark_size'] = watermark_settings.get('watermark_size', 'medium')
            form_data['watermark_opacity'] = str(watermark_settings.get('watermark_opacity', 30))
            if watermark_settings.get('watermark_box', False):
                form_data['watermark_box'] = 'on'
        
        response = requests.post(f"{SERVER_URL}/generate", data=form_data, timeout=600)
        response.raise_for_status()
        
        # Save video to temp file
        os.makedirs(TEMP_DIR, exist_ok=True)
        video_path = os.path.join(TEMP_DIR, f"video_{int(time.time())}.mp4")
        
        with open(video_path, 'wb') as f:
            f.write(response.content)
        
        if progress_callback:
            progress_callback("Video generated successfully")
        
        return video_path
    except Exception as e:
        print(f"Error generating video: {e}", file=sys.stderr)
        raise

def generate_subtitles(video_path, subtitle_settings, progress_callback=None):
    """Generate subtitles for video"""
    try:
        if progress_callback:
            progress_callback("Generating subtitles...")
        
        # Verify video file exists before uploading
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")
        
        # Read video file content first, then upload
        with open(video_path, 'rb') as f:
            video_content = f.read()
        
        # Prepare file for upload with explicit MIME type
        files = {'video': (os.path.basename(video_path), video_content, 'video/mp4')}
        data = {
            'max_words': str(subtitle_settings.get('max_words', 1)),
            'model_name': subtitle_settings.get('model_name', 'small'),
            'font_name': subtitle_settings.get('font_name', 'Fira Sans Ultra'),
            'font_size': str(subtitle_settings.get('font_size', 15)),
            'alignment': str(subtitle_settings.get('alignment', 2)),
            'margin_v': str(subtitle_settings.get('margin_v', 75)),
            'outline': str(subtitle_settings.get('outline', 0)),
            'shadow': str(subtitle_settings.get('shadow', 2)),
            'primary_color_hex': subtitle_settings.get('primary_color_hex', '&H007DD1F7&')
        }
        
        response = requests.post(f"{SERVER_URL}/generate-subtitles-gui", 
                               files=files, data=data, timeout=600,
                               headers={'X-Automation-Request': 'true'})
        response.raise_for_status()
        
        # Save subtitled video
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        video_name = os.path.basename(video_path)
        output_path = os.path.join(OUTPUT_DIR, f"{os.path.splitext(video_name)[0]}_subtitled.mp4")
        
        with open(output_path, 'wb') as f:
            f.write(response.content)
        
        if progress_callback:
            progress_callback("Subtitles generated successfully")
        
        return output_path
    except Exception as e:
        print(f"Error generating subtitles: {e}", file=sys.stderr)
        raise

def load_youtube_credentials():
    """Load YouTube credentials from file"""
    try:
        if os.path.exists(YOUTUBE_CREDENTIALS_FILE):
            with open(YOUTUBE_CREDENTIALS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading YouTube credentials: {e}", file=sys.stderr)
    return {}

def load_youtube_oauth_config():
    """Load YouTube OAuth config (Client ID/Secret) from file or env"""
    oauth_config_file = "youtube_oauth_config.json"
    try:
        if os.path.exists(oauth_config_file):
            with open(oauth_config_file, 'r') as f:
                data = json.load(f)
            # New multi-profile format: {profiles:[...], active:{clientId,clientSecret}}
            if 'active' in data and data['active'].get('clientId'):
                return data['active']
            # Old flat format: {clientId, clientSecret} at root
            if data.get('clientId'):
                return data
    except Exception as e:
        print(f"Error loading OAuth config: {e}", file=sys.stderr)

    # Fallback to environment variables
    return {
        'clientId': os.getenv('YOUTUBE_CLIENT_ID', ''),
        'clientSecret': os.getenv('YOUTUBE_CLIENT_SECRET', '')
    }

def validate_youtube_channels_before_upload(youtube_channels, excluded_channels=None):
    """
    Validate YouTube channels before video generation to prevent wasting credits.
    Checks authorization status and excludes channels that exceeded limits.
    Returns: (valid_channels, invalid_channels_info)
    """
    if excluded_channels is None:
        excluded_channels = set()
    
    valid_channels = []
    invalid_channels_info = []
    
    if not youtube_channels or len(youtube_channels) == 0:
        return valid_channels, invalid_channels_info
    
    credentials_data = load_youtube_credentials()
    oauth_config = load_youtube_oauth_config()
    
    if not oauth_config.get('clientId') or not oauth_config.get('clientSecret'):
        # All channels invalid - no OAuth config
        for channel_id in youtube_channels:
            invalid_channels_info.append({
                'channelId': channel_id,
                'reason': 'OAuth credentials not configured',
                'error': 'YOUTUBE_OAUTH_NOT_CONFIGURED'
            })
        return valid_channels, invalid_channels_info
    
    for channel_id in youtube_channels:
        # Skip channels that already exceeded limits
        if channel_id in excluded_channels:
            invalid_channels_info.append({
                'channelId': channel_id,
                'reason': 'Upload limit exceeded',
                'error': 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED'
            })
            continue
        
        # Check if channel is connected
        if channel_id not in credentials_data:
            invalid_channels_info.append({
                'channelId': channel_id,
                'reason': 'Channel not connected',
                'error': 'YOUTUBE_CHANNEL_NOT_CONNECTED'
            })
            continue
        
        channel_creds = credentials_data[channel_id]
        
        # Try to validate authorization by attempting to refresh token
        try:
            creds = Credentials(
                token=channel_creds.get('accessToken'),
                refresh_token=channel_creds.get('refreshToken'),
                token_uri='https://oauth2.googleapis.com/token',
                client_id=oauth_config['clientId'],
                client_secret=oauth_config['clientSecret']
            )
            
            # If expired, try to refresh
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    # If refresh succeeds, channel is valid
                    valid_channels.append(channel_id)
                except Exception as refresh_error:
                    error_msg = str(refresh_error)
                    if 'unauthorized_client' in error_msg.lower() or 'invalid_grant' in error_msg.lower():
                        invalid_channels_info.append({
                            'channelId': channel_id,
                            'channelTitle': channel_creds.get('channelTitle', channel_id),
                            'reason': 'Authorization expired or revoked',
                            'error': 'YOUTUBE_AUTHORIZATION_EXPIRED'
                        })
                    else:
                        # Other refresh error - still mark as invalid
                        invalid_channels_info.append({
                            'channelId': channel_id,
                            'channelTitle': channel_creds.get('channelTitle', channel_id),
                            'reason': f'Token refresh failed: {error_msg}',
                            'error': 'YOUTUBE_TOKEN_REFRESH_FAILED'
                        })
            else:
                # Token not expired, channel is valid
                valid_channels.append(channel_id)
        except Exception as e:
            invalid_channels_info.append({
                'channelId': channel_id,
                'channelTitle': channel_creds.get('channelTitle', channel_id),
                'reason': f'Validation error: {str(e)}',
                'error': 'YOUTUBE_VALIDATION_ERROR'
            })
    
    return valid_channels, invalid_channels_info

def get_youtube_service(channel_id):
    """Get authenticated YouTube service for a channel"""
    credentials_data = load_youtube_credentials()
    oauth_config = load_youtube_oauth_config()
    
    if channel_id not in credentials_data:
        raise ValueError(f"YouTube channel {channel_id} not connected. Please connect your channel first.")
    
    if not oauth_config.get('clientId') or not oauth_config.get('clientSecret'):
        raise ValueError("YouTube OAuth credentials not configured. Please set Client ID and Client Secret in the Automation tab.")
    
    channel_creds = credentials_data[channel_id]
    
    # Create credentials object
    creds = Credentials(
        token=channel_creds.get('accessToken'),
        refresh_token=channel_creds.get('refreshToken'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=oauth_config['clientId'],
        client_secret=oauth_config['clientSecret']
    )
    
    # Refresh token if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            
            # Update stored credentials
            channel_creds['accessToken'] = creds.token
            channel_creds['expiryDate'] = creds.expiry.timestamp() * 1000 if creds.expiry else None
            credentials_data[channel_id] = channel_creds
            
            # Save updated credentials
            with open(YOUTUBE_CREDENTIALS_FILE, 'w') as f:
                json.dump(credentials_data, f, indent=2)
        except Exception as refresh_error:
            error_msg = str(refresh_error)
            if 'unauthorized_client' in error_msg.lower() or 'invalid_grant' in error_msg.lower():
                raise ValueError(
                    f"❌ YouTube authorization expired or revoked for channel '{channel_creds.get('channelTitle', channel_id)}'.\n"
                    f"Please disconnect and reconnect this channel in the Automation tab.\n"
                    f"Error details: {error_msg}"
                )
            else:
                raise Exception(f"Failed to refresh YouTube token: {refresh_error}")
    
    return build('youtube', 'v3', credentials=creds)

def load_facebook_credentials():
    """Load Facebook/Instagram credentials from file"""
    try:
        if os.path.exists(FACEBOOK_CREDENTIALS_FILE):
            with open(FACEBOOK_CREDENTIALS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading Facebook credentials: {e}", file=sys.stderr)
    return {}

def get_facebook_page_credentials(page_id):
    """Get Facebook Page credentials"""
    credentials = load_facebook_credentials()
    page_key = f"page_{page_id}"
    if page_key not in credentials:
        raise ValueError(f"Facebook Page {page_id} not connected. Please connect your page first.")
    return credentials[page_key]

def get_instagram_account_credentials(account_id):
    """Get Instagram account credentials"""
    credentials = load_facebook_credentials()
    ig_key = f"ig_{account_id}"
    if ig_key not in credentials:
        raise ValueError(f"Instagram account {account_id} not connected. Please connect your account first.")
    return credentials[ig_key]



def _log_meta_error(response, context="Meta API"):
    """Log Meta/Facebook API error response body for debugging."""
    try:
        body = response.json() if response.text else {}
        err = body.get('error', body)
        msg = err.get('message', str(body)) if isinstance(err, dict) else str(body)
        code = err.get('code', '') if isinstance(err, dict) else ''
        subcode = err.get('error_subcode', '') if isinstance(err, dict) else ''
        print(f"[{context}] Error response: code={code} subcode={subcode} message={msg}", file=sys.stderr, flush=True)
        if isinstance(body, dict) and body.get('error_user_msg'):
            print(f"[{context}] User message: {body['error_user_msg']}", file=sys.stderr, flush=True)
    except Exception:
        print(f"[{context}] Raw response: {response.text[:500]}", file=sys.stderr, flush=True)


def upload_to_instagram(video_path, caption, instagram_account, progress_callback=None):
    """Upload Reel to Instagram using resumable upload (Meta requires video_url or resumable, not multipart to /media)."""
    try:
        if progress_callback:
            progress_callback("Uploading to Instagram...")
        
        creds = get_instagram_account_credentials(instagram_account)
        ig_account_id = creds['accountId']
        page_access_token = creds.get('pageAccessToken') or creds.get('page_access_token')
        
        with open(video_path, 'rb') as f:
            video_data = f.read()
        file_size = len(video_data)
        
        # Step 1: Create resumable upload container (no file yet)
        container_url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media"
        container_params = {
            'media_type': 'REELS',
            'caption': (caption or '')[:2200],
            'upload_type': 'resumable',
            'access_token': page_access_token
        }
        container_response = requests.post(container_url, data=container_params, timeout=60)
        if not container_response.ok:
            _log_meta_error(container_response, "Instagram container")
            container_response.raise_for_status()
        container_result = container_response.json()
        if 'id' not in container_result:
            raise Exception(f"Failed to create Instagram container: {container_result}")
        container_id = container_result['id']
        
        if progress_callback:
            progress_callback(f"Instagram container created: {container_id}")
        
        # Step 2: Upload video bytes to rupload.facebook.com (path: ig-api-upload/API_VERSION/CONTAINER_ID)
        rupload_url = f"https://rupload.facebook.com/ig-api-upload/v18.0/{container_id}"
        rupload_headers = {
            'Authorization': f'OAuth {page_access_token}',
            'offset': '0',
            'file_size': str(file_size),
            'Content-Type': 'application/octet-stream'
        }
        rupload_response = requests.post(rupload_url, data=video_data, headers=rupload_headers, timeout=600)
        if not rupload_response.ok:
            _log_meta_error(rupload_response, "Instagram rupload")
            rupload_response.raise_for_status()
        
        # Step 3: Poll container status until FINISHED
        status_url = f"https://graph.facebook.com/v18.0/{container_id}"
        max_wait = 300
        wait_time = 0
        status = None
        while wait_time < max_wait:
            status_response = requests.get(status_url, params={'fields': 'status_code', 'access_token': page_access_token}, timeout=30)
            if not status_response.ok:
                _log_meta_error(status_response, "Instagram status")
                status_response.raise_for_status()
            status_data = status_response.json()
            status = status_data.get('status_code')
            if status == 'FINISHED':
                break
            if status == 'ERROR':
                raise Exception(f"Instagram container processing failed: {status_data}")
            time.sleep(5)
            wait_time += 5
        
        if status != 'FINISHED':
            raise Exception(f"Instagram container timeout. Status: {status}")
        
        # Step 4: Publish the Reel
        publish_url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media_publish"
        publish_params = {'creation_id': container_id, 'access_token': page_access_token}
        publish_response = requests.post(publish_url, data=publish_params, timeout=60)
        if not publish_response.ok:
            _log_meta_error(publish_response, "Instagram publish")
            publish_response.raise_for_status()
        publish_result = publish_response.json()
        if 'id' not in publish_result:
            raise Exception(f"Failed to publish Instagram Reel: {publish_result}")
        reel_id = publish_result['id']
        
        if progress_callback:
            progress_callback(f"Instagram Reel published: {reel_id}")
        return {'success': True, 'reelId': reel_id, 'url': f'https://www.instagram.com/reel/{reel_id}/'}
        
    except requests.exceptions.HTTPError as e:
        if e.response is not None:
            _log_meta_error(e.response, "Instagram")
        print(f"Error uploading to Instagram: {e}", file=sys.stderr, flush=True)
        raise
    except Exception as e:
        print(f"Error uploading to Instagram: {e}", file=sys.stderr, flush=True)
        raise

def upload_to_facebook_page(video_path, title, description, publish_at, facebook_page, progress_callback=None):
    """Upload video to Facebook Page. Uses graph-video host for large/form uploads."""
    try:
        if progress_callback:
            progress_callback("Uploading to Facebook Page...")
        
        creds = get_facebook_page_credentials(facebook_page)
        page_id = creds['pageId']
        page_access_token = creds.get('pageAccessToken') or creds.get('page_access_token')
        
        with open(video_path, 'rb') as f:
            video_data = f.read()
        
        # graph-video.facebook.com is the correct host for Page video uploads (form/multipart)
        upload_url = f"https://graph-video.facebook.com/v18.0/{page_id}/videos"
        
        upload_params = {
            'title': (title or '')[:500],
            'description': (description or '')[:5000],
            'access_token': page_access_token
        }
        if publish_at:
            try:
                publish_dt = datetime.fromisoformat(publish_at.replace('Z', '+00:00'))
                publish_timestamp = int(publish_dt.timestamp())
                now_ts = int(datetime.now(timezone.utc).timestamp())
                min_ts = now_ts + FB_SCHEDULE_MIN_SECONDS_FROM_NOW
                max_ts = now_ts + (FB_SCHEDULE_MAX_DAYS_FROM_NOW * 24 * 3600)
                if min_ts <= publish_timestamp <= max_ts:
                    upload_params['published'] = 'false'
                    upload_params['scheduled_publish_time'] = str(publish_timestamp)
                else:
                    if publish_timestamp < min_ts:
                        print(f"Facebook: scheduled time is in the past or < 10 min from now; publishing immediately.", file=sys.stderr, flush=True)
                    else:
                        print(f"Facebook: scheduled time is > {FB_SCHEDULE_MAX_DAYS_FROM_NOW} days away; publishing immediately.", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"Warning: Could not parse publish_at, uploading immediately: {e}", file=sys.stderr, flush=True)
        
        files = {'source': (os.path.basename(video_path), video_data, 'video/mp4')}
        
        upload_response = requests.post(upload_url, data=upload_params, files=files, timeout=600)
        if not upload_response.ok:
            _log_meta_error(upload_response, "Facebook Page videos")
            upload_response.raise_for_status()
        upload_result = upload_response.json()
        if 'id' not in upload_result:
            raise Exception(f"Failed to upload to Facebook: {upload_result}")
        video_id = upload_result['id']
        
        if progress_callback:
            progress_callback(f"Facebook video uploaded: {video_id}")
        return {'success': True, 'videoId': video_id, 'url': f'https://www.facebook.com/{page_id}/videos/{video_id}/'}
        
    except requests.exceptions.HTTPError as e:
        if e.response is not None:
            _log_meta_error(e.response, "Facebook Page")
        print(f"Error uploading to Facebook: {e}", file=sys.stderr, flush=True)
        raise
    except Exception as e:
        print(f"Error uploading to Facebook: {e}", file=sys.stderr, flush=True)
        raise

def upload_to_facebook_photo(image_path, caption, facebook_page, publish_at=None, progress_callback=None):
    """Upload a photo to a Facebook Page using the Graph API /photos endpoint."""
    try:
        if progress_callback:
            progress_callback("Uploading photo to Facebook Page…")

        creds          = get_facebook_page_credentials(facebook_page)
        page_id        = creds['pageId']
        access_token   = creds.get('pageAccessToken') or creds.get('page_access_token')

        with open(image_path, 'rb') as f:
            image_data = f.read()

        params = {
            'caption':      (caption or '')[:5000],
            'access_token': access_token,
            'published':    'true',
        }

        if publish_at:
            try:
                from datetime import datetime, timezone
                dt  = datetime.fromisoformat(publish_at.replace('Z', '+00:00'))
                ts  = int(dt.timestamp())
                now = int(datetime.now(timezone.utc).timestamp())
                if ts > now + 600:
                    params['published']              = 'false'
                    params['scheduled_publish_time'] = str(ts)
            except Exception as e:
                print(f"Warning: could not parse publish_at ({e}); posting immediately.", file=sys.stderr, flush=True)

        ext   = os.path.splitext(image_path)[1].lower()
        ctype = 'image/jpeg' if ext in ('.jpg', '.jpeg') else 'image/png'
        files = {'source': (os.path.basename(image_path), image_data, ctype)}

        url      = f"https://graph.facebook.com/v18.0/{page_id}/photos"
        response = requests.post(url, data=params, files=files, timeout=120)
        if not response.ok:
            _log_meta_error(response, "Facebook Page photos")
            response.raise_for_status()
        result = response.json()
        if 'id' not in result:
            raise Exception(f"Facebook photo upload failed: {result}")

        if progress_callback:
            progress_callback(f"Facebook photo published: {result['id']}")
    except requests.exceptions.HTTPError as e:
        print(f"Error uploading photo to Facebook: {e}", file=sys.stderr, flush=True)
        raise
    except Exception as e:
        print(f"Error uploading photo to Facebook: {e}", file=sys.stderr, flush=True)
        raise


# Instagram schedule constants (same limits as Facebook)
_IG_SCHEDULE_MIN_SECONDS = 600      # 10 minutes
_IG_SCHEDULE_MAX_SECONDS = 75 * 24 * 3600  # 75 days


def _ig_apply_schedule(params: dict, publish_at: str | None, progress_callback=None) -> None:
    """
    Mutate *params* in-place to add Instagram scheduling fields when publish_at
    is a valid future time (≥10 min, ≤75 days from now).
    Does nothing when publish_at is absent or out of window.
    """
    if not publish_at:
        return
    try:
        _IST = timezone(timedelta(hours=5, minutes=30))
        dt = datetime.fromisoformat(str(publish_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        ts = int(dt.timestamp())
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if ts < now_ts + _IG_SCHEDULE_MIN_SECONDS:
            if progress_callback:
                progress_callback("Instagram: schedule time < 10 min from now — publishing immediately.")
            return
        if ts > now_ts + _IG_SCHEDULE_MAX_SECONDS:
            if progress_callback:
                progress_callback("Instagram: schedule time > 75 days away — publishing immediately.")
            return
        params["published"] = "false"
        params["scheduled_publish_time"] = str(ts)
        if progress_callback:
            progress_callback(f"Instagram: scheduled for {dt.isoformat()}")
    except Exception as e:
        print(f"[automation] _ig_apply_schedule: could not parse publish_at ({e}); posting immediately.", file=sys.stderr, flush=True)


def upload_to_instagram_photo(image_path, caption, instagram_account,
                               public_base_url=None, publish_at=None, progress_callback=None):
    """
    Upload a photo to Instagram Business via the Graph API.

    Instagram requires the image to be at a publicly accessible URL.
    Set PUBLIC_BASE_URL in .env (e.g. https://your-server.com) so Meta can
    fetch the image. If not set, this will log a warning and skip the upload.

    publish_at: ISO-8601 string (with or without timezone). If provided and ≥10 min
    in the future, the post is scheduled instead of published immediately.
    """
    try:
        if progress_callback:
            progress_callback("Uploading photo to Instagram…")

        base_url = public_base_url or os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not base_url:
            print(
                "[automation] Instagram photo upload requires PUBLIC_BASE_URL in .env "
                "(e.g. https://your-domain.com). Skipping Instagram upload.",
                file=sys.stderr, flush=True,
            )
            return

        creds          = get_instagram_account_credentials(instagram_account)
        ig_account_id  = creds['accountId']
        access_token   = creds.get('pageAccessToken') or creds.get('page_access_token')

        # Serve the image via the local server (needs PUBLIC_BASE_URL to be reachable by Meta)
        filename   = os.path.basename(image_path)
        image_url  = f"{base_url}/serve-tmp/{filename}"

        # Step 1: Create media container
        container_url    = f"https://graph.facebook.com/v18.0/{ig_account_id}/media"
        container_params = {
            'image_url':    image_url,
            'caption':      (caption or '')[:2200],
            'access_token': access_token,
        }
        _ig_apply_schedule(container_params, publish_at, progress_callback)
        r = requests.post(container_url, data=container_params, timeout=60)
        if not r.ok:
            _log_meta_error(r, "Instagram media container")
            r.raise_for_status()
        container_id = r.json().get('id')
        if not container_id:
            raise Exception(f"Failed to create Instagram media container: {r.json()}")

        if progress_callback:
            progress_callback(f"Instagram container created: {container_id}")

        # Step 2: Publish
        publish_url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media_publish"
        pub_params  = {'creation_id': container_id, 'access_token': access_token}
        pr = requests.post(publish_url, data=pub_params, timeout=60)
        if not pr.ok:
            _log_meta_error(pr, "Instagram media_publish")
            pr.raise_for_status()
        media_id = pr.json().get('id')
        if progress_callback:
            action = "scheduled" if container_params.get('published') == 'false' else "published"
            progress_callback(f"Instagram photo {action}: {media_id}")
    except requests.exceptions.HTTPError as e:
        print(f"Error uploading photo to Instagram: {e}", file=sys.stderr, flush=True)
        raise
    except Exception as e:
        print(f"Error uploading photo to Instagram: {e}", file=sys.stderr, flush=True)
        raise


def upload_carousel_to_instagram(image_paths, caption, instagram_account,
                                  public_base_url=None, publish_at=None, progress_callback=None):
    """
    Upload multiple images as an Instagram carousel post via the Graph API.

    Each image in image_paths must be accessible via a public URL.
    Set PUBLIC_BASE_URL in .env and ensure each file is in the uploads/ directory
    so server.js can serve it at /serve-tmp/<filename>.

    publish_at: ISO-8601 string (with or without timezone). If provided and ≥10 min
    in the future, the carousel is scheduled instead of published immediately.
    """
    try:
        if progress_callback:
            progress_callback(f"Uploading carousel ({len(image_paths)} slides) to Instagram…")

        base_url = public_base_url or os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not base_url:
            print(
                "[automation] Instagram carousel upload requires PUBLIC_BASE_URL in .env. Skipping.",
                file=sys.stderr, flush=True,
            )
            return

        creds         = get_instagram_account_credentials(instagram_account)
        ig_account_id = creds["accountId"]
        access_token  = creds.get("pageAccessToken") or creds.get("page_access_token")

        # Step 1: Create a carousel item container for each image
        item_ids = []
        for i, img_path in enumerate(image_paths):
            filename  = os.path.basename(img_path)
            image_url = f"{base_url}/serve-tmp/{filename}"
            url       = f"https://graph.facebook.com/v18.0/{ig_account_id}/media"
            r = requests.post(url, data={
                "image_url":        image_url,
                "is_carousel_item": "true",
                "access_token":     access_token,
            }, timeout=60)
            if not r.ok:
                _log_meta_error(r, f"carousel item {i}")
                r.raise_for_status()
            item_id = r.json().get("id")
            if not item_id:
                raise Exception(f"No id for carousel item {i}: {r.json()}")
            if progress_callback:
                progress_callback(f"  Slide {i + 1}/{len(image_paths)} container: {item_id}")
            item_ids.append(item_id)

        # Step 2: Create the carousel container
        carousel_params = {
            "media_type":   "CAROUSEL",
            "children":     ",".join(item_ids),
            "caption":      (caption or "")[:2200],
            "access_token": access_token,
        }
        _ig_apply_schedule(carousel_params, publish_at, progress_callback)
        url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media"
        r   = requests.post(url, data=carousel_params, timeout=60)
        if not r.ok:
            _log_meta_error(r, "carousel container")
            r.raise_for_status()
        carousel_id = r.json().get("id")
        if not carousel_id:
            raise Exception(f"Failed to create carousel container: {r.json()}")
        if progress_callback:
            progress_callback(f"Carousel container created: {carousel_id}")

        # Step 3: Publish
        pub_url = f"https://graph.facebook.com/v18.0/{ig_account_id}/media_publish"
        pr      = requests.post(pub_url, data={
            "creation_id":  carousel_id,
            "access_token": access_token,
        }, timeout=60)
        if not pr.ok:
            _log_meta_error(pr, "carousel publish")
            pr.raise_for_status()
        media_id = pr.json().get("id")
        if progress_callback:
            action = "scheduled" if carousel_params.get("published") == "false" else "published"
            progress_callback(f"Instagram carousel {action}: {media_id}")
    except requests.exceptions.HTTPError as e:
        print(f"Error uploading carousel to Instagram: {e}", file=sys.stderr, flush=True)
        raise
    except Exception as e:
        print(f"Error uploading carousel to Instagram: {e}", file=sys.stderr, flush=True)
        raise


def upload_to_youtube(video_path, title, description, publish_at, youtube_channel, progress_callback=None):
    """Upload video to YouTube using YouTube API directly"""
    try:
        if progress_callback:
            progress_callback("Uploading to YouTube...")
        
        # Get YouTube service
        youtube = get_youtube_service(youtube_channel)
        
        # Prepare video metadata
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'categoryId': '22',
                'tags': ['football', 'ronaldo', 'messi', 'neymar']
            },
            'status': {
                'privacyStatus': 'private',
                'publishAt': publish_at,
                'selfDeclaredMadeForKids': False
            }
        }
        
        # Upload video
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        insert_request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        # Execute upload with progress tracking
        response = None
        error = None
        retry = 0
        while response is None:
            try:
                status, response = insert_request.next_chunk()
                if response is not None:
                    if 'id' in response:
                        video_id = response['id']
                        if progress_callback:
                            progress_callback(f"YouTube upload successful: {video_id}")
                        return {
                            'success': True,
                            'videoId': video_id,
                            'url': f'https://www.youtube.com/watch?v={video_id}'
                        }
                    else:
                        raise Exception('Upload failed: No video ID in response')
            except HttpError as e:
                # Check for upload limit exceeded error
                error_details = str(e)
                if 'uploadLimitExceeded' in error_details or 'upload limit' in error_details.lower():
                    error_msg = "❌ YOUTUBE UPLOAD LIMIT EXCEEDED!\n"
                    error_msg += "You have reached the maximum number of videos you can upload.\n"
                    error_msg += "Please wait 24 hours or contact YouTube support to increase your limit.\n"
                    error_msg += "Automation stopped immediately."
                    print(error_msg, file=sys.stderr, flush=True)
                    # Raise a special exception that will stop the automation
                    raise Exception("YOUTUBE_UPLOAD_LIMIT_EXCEEDED")
                
                # Check for authorization errors
                if 'unauthorized' in error_details.lower() or e.resp.status == 401:
                    error_msg = "❌ YOUTUBE AUTHORIZATION ERROR!\n"
                    error_msg += "Your YouTube channel authorization has expired or been revoked.\n"
                    error_msg += "Please disconnect and reconnect your YouTube channel in the Automation tab.\n"
                    error_msg += f"Error: {error_details}"
                    print(error_msg, file=sys.stderr, flush=True)
                    raise Exception("YOUTUBE_AUTHORIZATION_EXPIRED")
                
                if e.resp.status in [500, 502, 503, 504]:
                    error = f"Retryable error: {e}"
                    retry += 1
                    if retry > 3:
                        raise Exception(f"Upload failed after retries: {error}")
                    time.sleep(2 ** retry)
                else:
                    raise Exception(f"YouTube upload failed: {e}")
            except Exception as e:
                error_str = str(e)
                # Check if it's an authorization error from get_youtube_service
                if 'YOUTUBE_AUTHORIZATION_EXPIRED' in error_str or 'authorization expired' in error_str.lower() or 'unauthorized' in error_str.lower():
                    if 'YOUTUBE_AUTHORIZATION_EXPIRED' not in error_str:
                        raise Exception("YOUTUBE_AUTHORIZATION_EXPIRED")
                raise Exception(f"YouTube upload error: {e}")
        
        raise Exception("Upload did not complete")
        
    except Exception as e:
        print(f"Error uploading to YouTube: {e}", file=sys.stderr)
        raise

def process_video_row(row, index, total, watermark_settings, subtitle_settings,
                     youtube_channels=None, instagram_accounts=None, facebook_pages=None,
                     use_platforms_from_csv=False,
                     use_watermark_text_from_csv=False,
                     progress_callback=None, excluded_channels=None,
                     review_before_post=False):
    """Process a single video row from CSV/Excel"""
    if excluded_channels is None:
        excluded_channels = set()
    try:
        script = str(row.get('script', '')).strip()
        title = str(row.get('title', f'Video {index + 1}')).strip()
        description = str(row.get('description', '')).strip()
        
        # Handle NaN values from pandas
        upload_date_raw = row.get('upload_date', '')
        upload_time_raw = row.get('upload_time', '')
        
        # Check for NaN values
        if pd.isna(upload_date_raw) or pd.isna(upload_time_raw):
            raise ValueError(f"Row {index + 1}: Missing upload_date or upload_time (found NaN values)")
        
        upload_date = str(upload_date_raw).strip()
        upload_time = str(upload_time_raw).strip()
        
        # Check for 'nan' string or empty values
        if upload_date.lower() == 'nan' or upload_time.lower() == 'nan' or not upload_date or not upload_time:
            raise ValueError(f"Row {index + 1}: Missing upload_date or upload_time")
        
        if not script:
            raise ValueError(f"Row {index + 1}: Missing script")
        if not title:
            raise ValueError(f"Row {index + 1}: Missing title")
        
        # Convert IST to UTC
        publish_at = convert_ist_to_utc(upload_date, upload_time)
        if not publish_at:
            raise ValueError(f"Row {index + 1}: Invalid date/time format (date: '{upload_date}', time: '{upload_time}')")
        
        # Platforms can be provided via UI config or per-row CSV columns
        youtube_creds = load_youtube_credentials()
        facebook_creds = load_facebook_credentials()

        if use_platforms_from_csv:
            yt_names = _split_multi_names(_get_row_value(row, ['youtube channel', 'youtube_channel', 'youtube channels', 'youtube_channels']))
            ig_names = _split_multi_names(_get_row_value(row, ['instagram page', 'instagram_account', 'instagram', 'instagram accounts', 'instagram_accounts']))
            fb_names = _split_multi_names(_get_row_value(row, ['facebook page', 'facebook_page', 'facebook', 'facebook pages', 'facebook_pages']))

            youtube_channels = _resolve_youtube_channel_ids(yt_names, youtube_creds)
            instagram_accounts = _resolve_instagram_account_ids(ig_names, facebook_creds)
            facebook_pages = _resolve_facebook_page_ids(fb_names, facebook_creds)

            if (not youtube_channels) and (not instagram_accounts) and (not facebook_pages):
                raise ValueError(
                    "No platforms provided in row. Provide at least one of: youtube channel, instagram page, facebook page."
                )

        # Normalize to lists (UI mode can still pass single values)
        if youtube_channels is None:
            youtube_channels = []
        if instagram_accounts is None:
            instagram_accounts = []
        if facebook_pages is None:
            facebook_pages = []
        if isinstance(instagram_accounts, str) and instagram_accounts.strip():
            instagram_accounts = [instagram_accounts.strip()]
        if isinstance(facebook_pages, str) and facebook_pages.strip():
            facebook_pages = [facebook_pages.strip()]

        # Track platforms that will be uploaded to
        platforms_list = []
        if youtube_channels and len(youtube_channels) > 0:
            platforms_list.append('YouTube')
        if instagram_accounts and len(instagram_accounts) > 0:
            platforms_list.append('Instagram')
        if facebook_pages and len(facebook_pages) > 0:
            platforms_list.append('Facebook')
        
        # Check for duplicates per platform/channel/page BEFORE video creation
        # This saves ElevenLabs credits by skipping video generation if all platforms are duplicates or exceeded limits
        filtered_youtube_channels = []
        filtered_instagram_accounts = []
        filtered_facebook_pages = []
        
        skipped_platforms = []
        
        # Check YouTube channels
        if youtube_channels and len(youtube_channels) > 0:
            for channel_id in youtube_channels:
                # Skip channels that already exceeded limits
                if channel_id in excluded_channels:
                    skipped_platforms.append(f"YouTube channel {channel_id[:8]}... (limit exceeded)")
                    continue
                    
                duplicate = check_duplicate(title, script, 'YouTube', channel_id)
                if duplicate:
                    duplicate_msg = f"Row {index + 1}: DUPLICATE DETECTED for YouTube channel {channel_id[:8]}...\n"
                    duplicate_msg += f"Title: '{title}'\n"
                    duplicate_msg += f"This video was already successfully uploaded to this channel on {duplicate.get('uploaded_datetime', 'unknown')}.\n"
                    duplicate_msg += f"Skipping upload to this channel only. (Note: Failed uploads are not considered duplicates and will be retried)"
                    print(duplicate_msg, file=sys.stderr, flush=True)
                    skipped_platforms.append(f"YouTube channel {channel_id[:8]}... (duplicate)")
                else:
                    filtered_youtube_channels.append(channel_id)
        
        # Check Instagram accounts
        if instagram_accounts and len(instagram_accounts) > 0:
            for account_id in instagram_accounts:
                duplicate = check_duplicate(title, script, 'Instagram', account_id)
                if duplicate:
                    duplicate_msg = f"Row {index + 1}: DUPLICATE DETECTED for Instagram account {account_id[:8]}...\n"
                    duplicate_msg += f"Title: '{title}'\n"
                    duplicate_msg += f"This video was already successfully uploaded to this account on {duplicate.get('uploaded_datetime', 'unknown')}.\n"
                    duplicate_msg += f"Skipping upload to this account only. (Note: Failed uploads are not considered duplicates and will be retried)"
                    print(duplicate_msg, file=sys.stderr, flush=True)
                    skipped_platforms.append(f"Instagram account {account_id[:8]}... (duplicate)")
                else:
                    filtered_instagram_accounts.append(account_id)
        
        # Check Facebook pages
        if facebook_pages and len(facebook_pages) > 0:
            for page_id in facebook_pages:
                duplicate = check_duplicate(title, script, 'Facebook', page_id)
                if duplicate:
                    duplicate_msg = f"Row {index + 1}: DUPLICATE DETECTED for Facebook page {page_id[:8]}...\n"
                    duplicate_msg += f"Title: '{title}'\n"
                    duplicate_msg += f"This video was already successfully uploaded to this page on {duplicate.get('uploaded_datetime', 'unknown')}.\n"
                    duplicate_msg += f"Skipping upload to this page only. (Note: Failed uploads are not considered duplicates and will be retried)"
                    print(duplicate_msg, file=sys.stderr, flush=True)
                    skipped_platforms.append(f"Facebook page {page_id[:8]}... (duplicate)")
                else:
                    filtered_facebook_pages.append(page_id)
        
        # If all platforms are duplicates or exceeded limits, skip video creation entirely
        if len(filtered_youtube_channels) == 0 and len(filtered_instagram_accounts) == 0 and len(filtered_facebook_pages) == 0:
            skip_msg = f"Row {index + 1}: SKIPPING VIDEO CREATION - All selected platforms are duplicates or exceeded limits!\n"
            skip_msg += f"Title: '{title}'\n"
            skip_msg += f"Skipped platforms: {', '.join(skipped_platforms)}\n"
            skip_msg += f"💡 Video creation skipped to save ElevenLabs credits."
            print(skip_msg, file=sys.stderr, flush=True)
            if progress_callback:
                progress_callback(f"⏭️ Skipping video {index + 1} - all platforms are duplicates or exceeded limits")
            return {
                'success': False,
                'index': index,
                'title': title,
                'error': 'ALL_PLATFORMS_DUPLICATE',
                'message': f"All selected platforms are duplicates or exceeded limits. Video creation skipped.",
                'skipped_platforms': skipped_platforms
            }
        
        # If some platforms were skipped, log it
        if skipped_platforms:
            print(f"⚠️ Row {index + 1}: Proceeding with non-duplicate platforms. Skipped: {', '.join(skipped_platforms)}", flush=True)
            if progress_callback:
                progress_callback(f"⚠️ Some platforms skipped (duplicates), continuing with remaining platforms...")
        
        # Add record to database BEFORE processing (to track all videos that will be processed)
        # Only create record if we're actually going to create the video
        add_video_record(
            title=title,
            script=script,
            description=description,
            platforms=platforms_list,
            scheduled_datetime=publish_at,
            created_datetime=datetime.now().isoformat(),
            source='automation',
            csv_data=dict(row) if row is not None else None,
        )
        
        # Generate video (only once) - only reaches here if at least one platform is not duplicate
        if progress_callback:
            progress_callback(f"🎬 Creating video for non-duplicate platforms...")
        # Watermark: use CSV text when "Use watermark text from CSV" is on; auto-enable if CSV has value
        watermark_settings_effective = dict(watermark_settings or {})
        if use_watermark_text_from_csv:
            wm_text = str(_get_row_value(row, ['watermark text', 'watermark_text', 'watermark'])).strip()
            if wm_text:
                watermark_settings_effective['watermark_text'] = wm_text
                watermark_settings_effective['enable_watermark'] = True  # Auto-enable when CSV has watermark
            elif watermark_settings_effective.get('enable_watermark', False):
                raise ValueError("Missing watermark text in row (CSV column: 'watermark text' / 'watermark').")
        # else: use UI defaults (enable_watermark and watermark_text from config)

        video_path = generate_video(script, watermark_settings_effective, 
                                   lambda msg: progress_callback(f"[{index + 1}/{total}] {msg}") if progress_callback else None)
        
        # Generate subtitles (only once)
        subtitled_path = generate_subtitles(video_path, subtitle_settings,
                                          lambda msg: progress_callback(f"[{index + 1}/{total}] {msg}") if progress_callback else None)
        
        # Upload to platforms (only non-duplicate ones)
        results = {}
        
        # Upload to YouTube channels (only non-duplicate ones, excluding channels that exceeded limits)
        if filtered_youtube_channels and len(filtered_youtube_channels) > 0:
            # Filter out channels that exceeded limits (already done above, but double-check)
            available_channels = [ch for ch in filtered_youtube_channels if ch not in excluded_channels]
            
            if len(available_channels) == 0:
                # All YouTube channels exceeded limits
                # But check if other platforms are available - if yes, continue with them
                if (filtered_instagram_accounts and len(filtered_instagram_accounts) > 0) or (filtered_facebook_pages and len(filtered_facebook_pages) > 0):
                    # Other platforms available - skip YouTube but continue
                    if progress_callback:
                        progress_callback(f"⚠️ All YouTube channels exceeded limits. Continuing with Instagram/Facebook...")
                    print(f"⚠️ Row {index + 1}: All YouTube channels exceeded limits. Skipping YouTube, continuing with other platforms.", file=sys.stderr, flush=True)
                    results['youtube'] = []
                else:
                    # No other platforms - this shouldn't happen if main() check worked, but handle it anyway
                    if progress_callback:
                        progress_callback(f"⚠️ All YouTube channels have exceeded upload limits. Skipping video {index + 1}.")
                    print(f"⚠️ Row {index + 1}: All YouTube channels have exceeded upload limits. Skipping video.", file=sys.stderr, flush=True)
                    results['youtube'] = []
                    return {
                        'success': False,
                        'index': index,
                        'title': title,
                        'error': 'ALL_YOUTUBE_CHANNELS_LIMIT_EXCEEDED',
                        'message': 'All YouTube channels have exceeded upload limits'
                    }
            else:
                # Some channels available - proceed with uploads
                # Log skipped channels
                skipped_channels = [ch for ch in filtered_youtube_channels if ch not in available_channels]
                if skipped_channels:
                    skipped_msg = f"⚠️ Row {index + 1}: Skipping YouTube channels that exceeded limits: {', '.join([ch[:8] + '...' for ch in skipped_channels])}"
                    print(skipped_msg, file=sys.stderr, flush=True)
                    if progress_callback:
                        progress_callback(skipped_msg)
                
                youtube_results = []
                limit_exceeded_channels = []
                
                for channel_id in available_channels:
                    try:
                        youtube_result = upload_to_youtube(subtitled_path, title, description, publish_at, 
                                                          channel_id,
                                                          lambda msg: progress_callback(f"[{index + 1}/{total}] YouTube[{channel_id[:8]}...]: {msg}") if progress_callback else None)
                        youtube_results.append({
                            'channelId': channel_id,
                            'result': youtube_result
                        })
                    except Exception as e:
                        error_str = str(e)
                        if 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED' in error_str:
                            # Channel exceeded limit - mark it
                            limit_exceeded_channels.append(channel_id)
                            limit_msg = f"❌ YouTube channel {channel_id[:8]}... exceeded upload limit. Will skip this channel for remaining videos."
                            print(limit_msg, file=sys.stderr, flush=True)
                            if progress_callback:
                                progress_callback(f"LIMIT_EXCEEDED: {channel_id}")
                            youtube_results.append({
                                'channelId': channel_id,
                                'error': 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED',
                                'limitExceeded': True
                            })
                        else:
                            print(f"❌ Error uploading to YouTube channel {channel_id}: {e}", file=sys.stderr, flush=True)
                            youtube_results.append({
                                'channelId': channel_id,
                                'error': str(e)
                            })
                
                results['youtube'] = youtube_results
                
                # Return limit exceeded channels so main() can track them
                if limit_exceeded_channels:
                    results['limit_exceeded_channels'] = limit_exceeded_channels
        
        # Upload to Instagram (only if not duplicate)
        if filtered_instagram_accounts:
            results['instagram'] = []
            caption = f"{title}\n\n{description}" if description else title
            for account_id in filtered_instagram_accounts:
                try:
                    instagram_result = upload_to_instagram(
                        subtitled_path,
                        caption,
                        account_id,
                        lambda msg: progress_callback(f"[{index + 1}/{total}] IG[{account_id[:8]}...]: {msg}") if progress_callback else None
                    )
                    results['instagram'].append({'accountId': account_id, 'result': instagram_result})
                except Exception as e:
                    print(f"❌ Error uploading to Instagram account {account_id}: {e}", file=sys.stderr, flush=True)
                    results['instagram'].append({'accountId': account_id, 'error': str(e)})
        
        # Upload to Facebook Pages (only if not duplicate)
        if filtered_facebook_pages:
            results['facebook'] = []
            for page_id in filtered_facebook_pages:
                try:
                    facebook_result = upload_to_facebook_page(
                        subtitled_path,
                        title,
                        description,
                        publish_at,
                        page_id,
                        lambda msg: progress_callback(f"[{index + 1}/{total}] FB[{page_id[:8]}...]: {msg}") if progress_callback else None
                    )
                    results['facebook'].append({'pageId': page_id, 'result': facebook_result})
                except Exception as e:
                    print(f"❌ Error uploading to Facebook page {page_id}: {e}", file=sys.stderr, flush=True)
                    results['facebook'].append({'pageId': page_id, 'error': str(e)})
        
        # Cleanup all files for this video after successful upload
        print(f"🧹 Cleaning up files for video {index + 1}...", flush=True)
        cleanup_count = 0
        
        # Clean up original video file
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
                cleanup_count += 1
                print(f"   ✅ Deleted: {video_path}", flush=True)
            except Exception as e:
                print(f"   ⚠️ Could not delete {video_path}: {e}", flush=True)
        
        # Clean up subtitled video file
        if os.path.exists(subtitled_path):
            try:
                os.remove(subtitled_path)
                cleanup_count += 1
                print(f"   ✅ Deleted: {subtitled_path}", flush=True)
            except Exception as e:
                print(f"   ⚠️ Could not delete {subtitled_path}: {e}", flush=True)
        
        # Clean up any files in uploads folder that match this video's pattern
        video_name = os.path.basename(video_path)
        video_base = os.path.splitext(video_name)[0]
        if os.path.exists(TEMP_DIR):
            for filename in os.listdir(TEMP_DIR):
                # Match files that might be related to this video
                if video_base in filename or filename.endswith('.mp4'):
                    file_path = os.path.join(TEMP_DIR, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            cleanup_count += 1
                            print(f"   ✅ Deleted: {file_path}", flush=True)
                    except Exception as e:
                        print(f"   ⚠️ Could not delete {file_path}: {e}", flush=True)
        
        # Clean up any files in output folder that match this video's pattern
        if os.path.exists(OUTPUT_DIR):
            subtitled_abs = os.path.abspath(subtitled_path)
            for filename in os.listdir(OUTPUT_DIR):
                # Match files that might be related to this video
                if video_base in filename:
                    file_path = os.path.join(OUTPUT_DIR, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            cleanup_count += 1
                            print(f"   ✅ Deleted: {file_path}", flush=True)
                    except Exception as e:
                        print(f"   ⚠️ Could not delete {file_path}: {e}", flush=True)
        
        print(f"✅ Cleaned up {cleanup_count} files for video {index + 1}", flush=True)
        
        # Track platforms that were successfully uploaded to (with specific IDs and names)
        platforms_uploaded_details = {
            'youtube_channels': [],
            'youtube_channel_names': {},  # Map channel_id -> channel_title
            'instagram_accounts': [],
            'instagram_account_names': {},  # Map account_id -> username
            'facebook_pages': [],
            'facebook_page_names': {}  # Map page_id -> page_name
        }
        
        # Load credentials to get channel/page names
        youtube_creds = load_youtube_credentials()
        facebook_creds = load_facebook_credentials()
        
        if 'youtube' in results:
            if isinstance(results['youtube'], list):
                for yt_result in results['youtube']:
                    # Only mark as uploaded if upload was successful
                    # Success structure: {'channelId': id, 'result': {'success': True, ...}}
                    # Error structure: {'channelId': id, 'error': '...'}
                    if 'result' in yt_result and yt_result['result'].get('success') and 'error' not in yt_result:
                        channel_id = yt_result['channelId']
                        platforms_uploaded_details['youtube_channels'].append(channel_id)
                        # Get channel title from credentials
                        if channel_id in youtube_creds:
                            channel_title = youtube_creds[channel_id].get('channelTitle', channel_id)
                            platforms_uploaded_details['youtube_channel_names'][channel_id] = channel_title
                        else:
                            platforms_uploaded_details['youtube_channel_names'][channel_id] = channel_id
            elif results['youtube'].get('success') and 'error' not in results['youtube']:
                # Single channel (backward compatibility)
                if filtered_youtube_channels and len(filtered_youtube_channels) > 0:
                    channel_id = filtered_youtube_channels[0]
                    platforms_uploaded_details['youtube_channels'].append(channel_id)
                    if channel_id in youtube_creds:
                        channel_title = youtube_creds[channel_id].get('channelTitle', channel_id)
                        platforms_uploaded_details['youtube_channel_names'][channel_id] = channel_title
                    else:
                        platforms_uploaded_details['youtube_channel_names'][channel_id] = channel_id
        
        if 'instagram' in results and isinstance(results['instagram'], list):
            for ig_res in results['instagram']:
                account_id = ig_res.get('accountId')
                if not account_id:
                    continue
                # Only count actually posted Reels — queued/future jobs must not update DB here.
                ok = bool(ig_res.get('result') and ig_res['result'].get('success'))
                if not ok:
                    continue
                platforms_uploaded_details['instagram_accounts'].append(account_id)
                ig_key = f"ig_{account_id}"
                if ig_key in facebook_creds:
                    username = facebook_creds[ig_key].get('username', account_id)
                    platforms_uploaded_details['instagram_account_names'][account_id] = username
                else:
                    platforms_uploaded_details['instagram_account_names'][account_id] = account_id
        
        if 'facebook' in results and isinstance(results['facebook'], list):
            for fb_res in results['facebook']:
                page_id = fb_res.get('pageId')
                if not page_id:
                    continue
                if fb_res.get('result') and fb_res['result'].get('success'):
                    platforms_uploaded_details['facebook_pages'].append(page_id)
                    page_key = f"page_{page_id}"
                    if page_key in facebook_creds:
                        page_name = facebook_creds[page_key].get('pageName', page_id)
                        platforms_uploaded_details['facebook_page_names'][page_id] = page_name
                    else:
                        platforms_uploaded_details['facebook_page_names'][page_id] = page_id
        
        # Store social caption for the Notion comment (description = post text).
        if description:
            platforms_uploaded_details['notion_caption'] = description

        # Only mark as uploaded if at least one platform successfully uploaded
        if (platforms_uploaded_details['youtube_channels'] or
            platforms_uploaded_details['instagram_accounts'] or
            platforms_uploaded_details['facebook_pages']):
            mark_video_uploaded(title, script, platforms_uploaded_details)
            print(f"✅ Marked video '{title}' as uploaded to: {len(platforms_uploaded_details['youtube_channels'])} YouTube channel(s), {len(platforms_uploaded_details['instagram_accounts'])} Instagram account(s), {len(platforms_uploaded_details['facebook_pages'])} Facebook page(s)", flush=True)
        else:
            print(f"⚠️ Video '{title}' was not marked as uploaded - no successful uploads to any platform", flush=True)
        
        # Build result object
        result = {
            'success': True,
            'index': index,
            'title': title,
            'scheduledFor': publish_at
        }
        
        # Add platform-specific results
        if 'youtube' in results:
            # Handle multiple YouTube channels
            if isinstance(results['youtube'], list):
                result['youtube'] = []
                for yt_result in results['youtube']:
                    if 'result' in yt_result:
                        result['youtube'].append({
                            'channelId': yt_result['channelId'],
                            'videoId': yt_result['result'].get('videoId', 'pending'),
                            'url': yt_result['result'].get('url', '')
                        })
                    else:
                        result['youtube'].append({
                            'channelId': yt_result['channelId'],
                            'error': yt_result.get('error', 'Unknown error')
                        })
            else:
                # Single channel (backward compatibility)
                result['youtube'] = {
                    'videoId': results['youtube'].get('videoId', 'pending'),
                    'url': results['youtube'].get('url', '')
                }
        if 'instagram' in results:
            # Multiple accounts supported
            if isinstance(results['instagram'], list):
                result['instagram'] = results['instagram']
            else:
                result['instagram'] = {
                    'reelId': results['instagram'].get('reelId', 'pending'),
                    'url': results['instagram'].get('url', '')
                }
        if 'facebook' in results:
            # Multiple pages supported
            if isinstance(results['facebook'], list):
                result['facebook'] = results['facebook']
            else:
                result['facebook'] = {
                    'videoId': results['facebook'].get('videoId', 'pending'),
                    'url': results['facebook'].get('url', '')
                }
        
        return result
    except Exception as e:
        return {
            'success': False,
            'index': index,
            'error': str(e)
        }

def parse_file(file_path):
    """Parse CSV or Excel file"""
    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_path.endswith('.xlsx') or file_path.endswith('.xls'):
            df = pd.read_excel(file_path)
        else:
            raise ValueError("Unsupported file format. Use CSV or Excel files.")
        
        # Normalize column names (lowercase, strip whitespace)
        df.columns = df.columns.str.lower().str.strip()
        
        # Filter out empty rows - check for required columns
        required_columns = ['script', 'title', 'upload_date', 'upload_time']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            raise ValueError(f"Missing required columns in CSV/Excel: {', '.join(missing_columns)}")
        
        # Filter out rows with missing required fields
        df = df[
            df['script'].notna() & 
            df['title'].notna() & 
            df['upload_date'].notna() & 
            df['upload_time'].notna()
        ]
        
        # Also filter out rows where date/time are 'nan' strings
        df = df[
            (df['upload_date'].astype(str).str.lower() != 'nan') &
            (df['upload_time'].astype(str).str.lower() != 'nan') &
            (df['upload_date'].astype(str).str.strip() != '') &
            (df['upload_time'].astype(str).str.strip() != '')
        ]
        
        # Return records (keep any optional platform columns if present)
        records = df.to_dict('records')
        # Normalize keys (already normalized by df.columns), ensure None/NaN -> ''
        for rec in records:
            for k, v in list(rec.items()):
                if v is None:
                    rec[k] = ''
                else:
                    try:
                        if isinstance(v, float) and pd.isna(v):
                            rec[k] = ''
                    except Exception:
                        pass
        return records
    except Exception as e:
        print(f"Error parsing file: {e}", file=sys.stderr)
        raise


def _split_multi_names(value):
    """Split CSV cell into list of names/ids (comma/semicolon/pipe separated)."""
    if value is None:
        return []
    s = str(value).strip()
    if not s or s.lower() == 'nan':
        return []
    # Normalize separators
    for sep in [';', '|', '\n']:
        s = s.replace(sep, ',')
    parts = [p.strip() for p in s.split(',')]
    return [p for p in parts if p]


def _get_row_value(row: dict, keys: list[str]):
    """Get first non-empty value from row for given keys (case-insensitive)."""
    if not isinstance(row, dict):
        return ''
    lower_map = {str(k).strip().lower(): k for k in row.keys()}
    for key in keys:
        lk = key.strip().lower()
        if lk in lower_map:
            v = row.get(lower_map[lk])
            if v is None:
                continue
            vs = str(v).strip()
            if vs and vs.lower() != 'nan':
                return v
    return ''


def _resolve_youtube_channel_ids(names_or_ids, youtube_creds: dict) -> list[str]:
    """Resolve YouTube channel titles (or IDs) to channel IDs using youtube_credentials.json."""
    if not names_or_ids:
        return []
    title_to_id = {}
    for cid, meta in (youtube_creds or {}).items():
        title = (meta.get('channelTitle') or '').strip()
        if title:
            title_to_id[title.lower()] = cid
    resolved = []
    for raw in names_or_ids:
        s = str(raw).strip()
        if not s:
            continue
        # If it's already a connected channelId, accept it
        if s in (youtube_creds or {}):
            resolved.append(s)
            continue
        # Match by title
        cid = title_to_id.get(s.lower())
        if cid:
            resolved.append(cid)
            continue
        raise ValueError(f"Unknown YouTube channel '{s}'. Connect it first (exact title match).")
    # de-dupe while preserving order
    out = []
    for x in resolved:
        if x not in out:
            out.append(x)
    return out


def _resolve_instagram_account_ids(names_or_ids, facebook_creds: dict) -> list[str]:
    """Resolve Instagram usernames (or account IDs) to connected account IDs."""
    if not names_or_ids:
        return []
    username_to_id = {}
    for key, meta in (facebook_creds or {}).items():
        if not isinstance(key, str) or not key.startswith('ig_'):
            continue
        account_id = key[len('ig_'):]
        username = (meta.get('username') or '').strip().lstrip('@')
        if username:
            username_to_id[username.lower()] = account_id
    resolved = []
    for raw in names_or_ids:
        s = str(raw).strip()
        if not s:
            continue
        s_noat = s.lstrip('@')
        # If it's already a connected accountId, accept
        if f"ig_{s_noat}" in (facebook_creds or {}):
            resolved.append(s_noat)
            continue
        acct = username_to_id.get(s_noat.lower())
        if acct:
            resolved.append(acct)
            continue
        raise ValueError(f"Unknown Instagram account '{s}'. Connect it first (exact @username match).")
    out = []
    for x in resolved:
        if x not in out:
            out.append(x)
    return out


def _resolve_facebook_page_ids(names_or_ids, facebook_creds: dict) -> list[str]:
    """Resolve Facebook page names (or page IDs) to connected page IDs."""
    if not names_or_ids:
        return []
    name_to_id = {}
    for key, meta in (facebook_creds or {}).items():
        if not isinstance(key, str) or not key.startswith('page_'):
            continue
        page_id = key[len('page_'):]
        name = (meta.get('pageName') or '').strip()
        if name:
            name_to_id[name.lower()] = page_id
    resolved = []
    for raw in names_or_ids:
        s = str(raw).strip()
        if not s:
            continue
        # If it's already a connected pageId, accept
        if f"page_{s}" in (facebook_creds or {}):
            resolved.append(s)
            continue
        pid = name_to_id.get(s.lower())
        if pid:
            resolved.append(pid)
            continue
        raise ValueError(f"Unknown Facebook page '{s}'. Connect it first (exact page name match).")
    out = []
    for x in resolved:
        if x not in out:
            out.append(x)
    return out

def main():
    """Main automation function"""
    if len(sys.argv) < 2:
        print("Usage: python3 automation.py <config_json>", file=sys.stderr)
        sys.exit(1)
    
    try:
        # Load environment variables for YouTube API
        from dotenv import load_dotenv
        load_dotenv()
        
        # Load configuration from JSON
        config_path = sys.argv[1]
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        file_path = config['file_path']
        watermark_settings = config.get('watermark_settings', {})
        subtitle_settings = config.get('subtitle_settings', {})
        youtube_channels = config.get('youtube_channels')  # Can be None or list
        instagram_account = config.get('instagram_account')
        facebook_page = config.get('facebook_page')
        use_platforms_from_csv = bool(config.get('use_platforms_from_csv', False))
        use_watermark_text_from_csv = bool(config.get('use_watermark_text_from_csv', False))
        review_before_post = bool(config.get('review_before_post', False))
        
        # Validate at least one platform is selected (unless using per-row CSV platforms)
        if (not use_platforms_from_csv and (not youtube_channels or len(youtube_channels) == 0)
                and not instagram_account and not facebook_page):
            print("ERROR: No platform selected. Please select at least one platform (YouTube, Instagram, or Facebook).", file=sys.stderr)
            sys.exit(1)
        
        # Validate YouTube channels if selected (UI mode)
        if (not use_platforms_from_csv) and youtube_channels and len(youtube_channels) > 0:
            oauth_config = load_youtube_oauth_config()
            if not oauth_config.get('clientId') or not oauth_config.get('clientSecret'):
                print("ERROR: YouTube OAuth credentials not configured.", file=sys.stderr)
                print("Please set Client ID and Client Secret in the Automation tab.", file=sys.stderr)
                sys.exit(1)
            
            credentials = load_youtube_credentials()
            for channel_id in youtube_channels:
                if channel_id not in credentials:
                    print(f"ERROR: YouTube channel {channel_id} not connected.", file=sys.stderr)
                    print("Please connect your YouTube channel first using the web interface.", file=sys.stderr)
                    sys.exit(1)
        
        # Validate Instagram if selected (UI mode)
        if (not use_platforms_from_csv) and instagram_account:
            credentials = load_facebook_credentials()
            ig_key = f"ig_{instagram_account}"
            if ig_key not in credentials:
                print(f"ERROR: Instagram account {instagram_account} not connected.", file=sys.stderr)
                print("Please connect your Instagram account first using the web interface.", file=sys.stderr)
                sys.exit(1)
        
        # Validate Facebook Page if selected (UI mode)
        if (not use_platforms_from_csv) and facebook_page:
            credentials = load_facebook_credentials()
            page_key = f"page_{facebook_page}"
            if page_key not in credentials:
                print(f"ERROR: Facebook Page {facebook_page} not connected.", file=sys.stderr)
                print("Please connect your Facebook Page first using the web interface.", file=sys.stderr)
                sys.exit(1)
        
        # Parse file
        print(f"Parsing file: {file_path}", flush=True)
        video_data = parse_file(file_path)
        total = len(video_data)
        
        print(f"Found {total} videos to process", flush=True)
        
        results = []
        
        # Track channels that exceeded upload limits
        excluded_channels = set()
        
        # Process each video
        for i, row in enumerate(video_data):
            # Before processing, validate YouTube channels (check authorization and limits)
            # This prevents wasting ElevenLabs credits if channels are invalid
            validated_youtube_channels = youtube_channels.copy() if (youtube_channels and not use_platforms_from_csv) else []
            invalid_channels_info = []
            
            if (not use_platforms_from_csv) and youtube_channels and len(youtube_channels) > 0:
                valid_channels, invalid_channels_info = validate_youtube_channels_before_upload(
                    youtube_channels, excluded_channels=excluded_channels
                )
                
                # Log invalid channels
                for invalid_info in invalid_channels_info:
                    channel_id = invalid_info['channelId']
                    channel_title = invalid_info.get('channelTitle', channel_id[:8] + '...')
                    reason = invalid_info['reason']
                    error_type = invalid_info['error']
                    
                    if error_type == 'YOUTUBE_AUTHORIZATION_EXPIRED':
                        print(f"⚠️ Row {i + 1}: YouTube channel '{channel_title}' authorization expired. Skipping this channel.", file=sys.stderr, flush=True)
                        excluded_channels.add(channel_id)  # Add to excluded to prevent retry
                    elif error_type == 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED':
                        print(f"⚠️ Row {i + 1}: YouTube channel '{channel_title}' exceeded upload limit. Skipping this channel.", file=sys.stderr, flush=True)
                    else:
                        print(f"⚠️ Row {i + 1}: YouTube channel '{channel_title}' invalid: {reason}. Skipping this channel.", file=sys.stderr, flush=True)
                        excluded_channels.add(channel_id)  # Add to excluded
                
                validated_youtube_channels = valid_channels
            
            # Before processing, check if all platforms are unavailable
            # Only stop if ALL platforms (YouTube + Instagram + Facebook) are unavailable
            
            # Check YouTube channels availability
            youtube_available = False
            if validated_youtube_channels and len(validated_youtube_channels) > 0:
                available_channels = [ch for ch in validated_youtube_channels if ch not in excluded_channels]
                youtube_available = len(available_channels) > 0
            elif not validated_youtube_channels or len(validated_youtube_channels) == 0:
                # No YouTube channels selected or all invalid, so YouTube is not a factor
                youtube_available = False  # Not selected, so doesn't count
            
            # Check other platforms
            instagram_available = bool(instagram_account) and not use_platforms_from_csv
            facebook_available = bool(facebook_page) and not use_platforms_from_csv
            
            # Only stop if ALL platforms are unavailable
            # If YouTube was selected but all channels exceeded/expired, AND no other platforms selected
            if (not use_platforms_from_csv) and youtube_channels and len(youtube_channels) > 0 and not youtube_available and not instagram_available and not facebook_available:
                # All YouTube channels invalid AND no other platforms - stop BEFORE video generation
                print("\n" + "="*60, flush=True)
                if len(youtube_channels) == 1:
                    print("🛑 SKIPPING VIDEO CREATION - YOUTUBE CHANNEL INVALID", flush=True)
                    print(f"Single channel selected: {youtube_channels[0][:8]}...", flush=True)
                    if invalid_channels_info:
                        print(f"Reason: {invalid_channels_info[0]['reason']}", flush=True)
                else:
                    print("🛑 SKIPPING VIDEO CREATION - ALL YOUTUBE CHANNELS INVALID", flush=True)
                    print(f"All {len(youtube_channels)} channels are invalid (authorization expired or limit exceeded).", flush=True)
                print("="*60, flush=True)
                print(f"Processed {i} videos before channels became invalid.", flush=True)
                print(f"Videos {i + 1} to {total} were not processed (saved ElevenLabs credits).", flush=True)
                if excluded_channels:
                    print(f"Excluded channels: {', '.join([ch[:8] + '...' for ch in excluded_channels])}", flush=True)
                print(f"PROGRESS: LIMIT_EXCEEDED_ALL: All YouTube channels invalid. Video creation skipped.", flush=True)
                results.append({
                    'success': False,
                    'index': i,
                    'error': 'ALL_YOUTUBE_CHANNELS_INVALID',
                    'message': f'All YouTube channels invalid (authorization expired or limit exceeded). Processed {i} videos.',
                    'excluded_channels': list(excluded_channels),
                    'invalid_channels': invalid_channels_info
                })
                break
            elif (not use_platforms_from_csv) and youtube_channels and len(youtube_channels) > 0 and not youtube_available:
                # Some YouTube channels invalid, but other platforms available - continue
                print(f"⚠️ All YouTube channels invalid, but continuing with other platforms (Instagram/Facebook)...", flush=True)
                print(f"PROGRESS: LIMIT_EXCEEDED_YOUTUBE_ONLY: All YouTube channels invalid, continuing with other platforms.", flush=True)
            
            print(f"Processing video {i + 1}/{total}: {row.get('title', 'Untitled')}", flush=True)
            
            try:
                result = process_video_row(
                    row, i, total,
                    watermark_settings,
                    subtitle_settings,
                    validated_youtube_channels,  # Use validated channels instead of original
                    [instagram_account] if (instagram_account and not use_platforms_from_csv) else [],
                    [facebook_page] if (facebook_page and not use_platforms_from_csv) else [],
                    use_platforms_from_csv=use_platforms_from_csv,
                    use_watermark_text_from_csv=use_watermark_text_from_csv,
                    progress_callback=lambda msg: print(f"PROGRESS: {msg}", flush=True),
                    excluded_channels=excluded_channels,
                    review_before_post=review_before_post
                )
                
                results.append(result)
                
                # Track channels that exceeded limits or had authorization errors
                if 'youtube' in result and isinstance(result.get('youtube'), list):
                    for yt_result in result['youtube']:
                        if yt_result.get('limitExceeded') or (yt_result.get('error') and 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED' in str(yt_result.get('error'))):
                            channel_id = yt_result.get('channelId')
                            if channel_id:
                                excluded_channels.add(channel_id)
                                print(f"📝 Added channel {channel_id[:8]}... to excluded list (limit exceeded)", flush=True)
                                print(f"PROGRESS: LIMIT_EXCEEDED: {channel_id}", flush=True)
                        elif yt_result.get('error') and 'YOUTUBE_AUTHORIZATION_EXPIRED' in str(yt_result.get('error')):
                            channel_id = yt_result.get('channelId')
                            if channel_id:
                                excluded_channels.add(channel_id)
                                print(f"📝 Added channel {channel_id[:8]}... to excluded list (authorization expired)", flush=True)
                                print(f"PROGRESS: AUTHORIZATION_EXPIRED: {channel_id}", flush=True)
                
                # Also check limit_exceeded_channels from results
                if 'limit_exceeded_channels' in result:
                    for channel_id in result['limit_exceeded_channels']:
                        excluded_channels.add(channel_id)
                        print(f"📝 Added channel {channel_id[:8]}... to excluded list (limit exceeded)", flush=True)
                        print(f"PROGRESS: LIMIT_EXCEEDED: {channel_id}", flush=True)
                
                # Check if all channels exceeded (single channel case)
                if not result.get('success') and result.get('error') == 'ALL_YOUTUBE_CHANNELS_LIMIT_EXCEEDED':
                    # This means all channels exceeded - already handled above, but double-check
                    if youtube_channels and len(youtube_channels) == 1:
                        # Only one channel was selected and it exceeded - stop
                        print("\n" + "="*60, flush=True)
                        print("🛑 AUTOMATION STOPPED - SINGLE YOUTUBE CHANNEL EXCEEDED UPLOAD LIMIT", flush=True)
                        print("="*60, flush=True)
                        print(f"Processed {i} videos before hitting the limit.", flush=True)
                        print(f"Videos {i + 1} to {total} were not processed (saved ElevenLabs credits).", flush=True)
                        print(f"PROGRESS: LIMIT_EXCEEDED_ALL: Single YouTube channel exceeded upload limit. Automation stopped.", flush=True)
                        break
                
                # Check if authorization expired - stop immediately
                if not result.get('success') and 'YOUTUBE_AUTHORIZATION_EXPIRED' in str(result.get('error', '')):
                    print("\n" + "="*60, flush=True)
                    print("🛑 AUTOMATION STOPPED DUE TO YOUTUBE AUTHORIZATION ERROR", flush=True)
                    print("="*60, flush=True)
                    print(f"Processed {i} videos before authorization expired.", flush=True)
                    print(f"Please disconnect and reconnect your YouTube channel, then restart automation.", flush=True)
                    print(f"Videos {i + 1} to {total} were not processed.", flush=True)
                    break
                
            except KeyboardInterrupt:
                print("\n" + "="*60, flush=True)
                print("🛑 AUTOMATION STOPPED BY USER", flush=True)
                print("="*60, flush=True)
                print(f"Processed {i} videos before stopping.", flush=True)
                print(f"Videos {i + 1} to {total} were not processed.", flush=True)
                results.append({
                    'success': False,
                    'index': i,
                    'error': 'Stopped by user'
                })
                break
            except Exception as e:
                error_msg = str(e)
                if 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED' in error_msg:
                    print("\n" + "="*60, flush=True)
                    print("🛑 AUTOMATION STOPPED DUE TO YOUTUBE UPLOAD LIMIT", flush=True)
                    print("="*60, flush=True)
                    print(f"Processed {i} videos before hitting the limit.", flush=True)
                    print(f"Videos {i + 1} to {total} were not processed.", flush=True)
                    results.append({
                        'success': False,
                        'index': i,
                        'error': error_msg
                    })
                    break
                elif 'YOUTUBE_AUTHORIZATION_EXPIRED' in error_msg:
                    print("\n" + "="*60, flush=True)
                    print("🛑 AUTOMATION STOPPED DUE TO YOUTUBE AUTHORIZATION ERROR", flush=True)
                    print("="*60, flush=True)
                    print(f"Processed {i} videos before authorization expired.", flush=True)
                    print(f"Please disconnect and reconnect your YouTube channel, then restart automation.", flush=True)
                    print(f"Videos {i + 1} to {total} were not processed.", flush=True)
                    results.append({
                        'success': False,
                        'index': i,
                        'error': error_msg
                    })
                    break
                else:
                    # Other errors - continue processing
                    results.append({
                        'success': False,
                        'index': i,
                        'error': error_msg
                    })
                    print(f"❌ Error processing video {i + 1}: {error_msg}", flush=True)
                    continue
            
            # Small delay between videos
            if i < total - 1:
                time.sleep(2)
        
        # Output results as JSON
        output = {
            'total': total,
            'successful': sum(1 for r in results if r.get('success')),
            'failed': sum(1 for r in results if not r.get('success')),
            'results': results
        }
        
        print(f"\n=== AUTOMATION COMPLETE ===", flush=True)
        print(f"Total: {output['total']}", flush=True)
        print(f"Successful: {output['successful']}", flush=True)
        print(f"Failed: {output['failed']}", flush=True)
        
        # Final cleanup pass - remove any remaining temporary files (safety net)
        # Note: Files should already be cleaned up after each video upload, this is just a safety net
        print("\n🧹 Final cleanup pass (removing any remaining temporary files)...", flush=True)
        cleanup_count = 0
        
        # Clean up uploads folder (files uploaded to server during subtitle generation)
        uploads_dir = TEMP_DIR
        if os.path.exists(uploads_dir):
            for filename in os.listdir(uploads_dir):
                file_path = os.path.join(uploads_dir, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        cleanup_count += 1
                        print(f"   ✅ Deleted: {file_path}", flush=True)
                except Exception as e:
                    print(f"   ⚠️ Could not delete {file_path}: {e}", flush=True)
        
        # Clean up output folder (subtitled videos that were already uploaded)
        if os.path.exists(OUTPUT_DIR):
            for filename in os.listdir(OUTPUT_DIR):
                # Skip results JSON files
                if filename.startswith('automation_results_'):
                    continue
                file_path = os.path.join(OUTPUT_DIR, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        cleanup_count += 1
                        print(f"   ✅ Deleted: {file_path}", flush=True)
                except Exception as e:
                    print(f"   ⚠️ Could not delete {file_path}: {e}", flush=True)
        
        if cleanup_count > 0:
            print(f"✅ Final cleanup: removed {cleanup_count} remaining temporary files", flush=True)
        else:
            print("✅ No remaining temporary files to clean up", flush=True)
        
        # Write results to file
        results_file = os.path.join(OUTPUT_DIR, f"automation_results_{int(time.time())}.json")
        with open(results_file, 'w') as f:
            json.dump(output, f, indent=2)
        
        print(f"Results saved to: {results_file}", flush=True)
        
        # Print JSON output for Node.js to capture
        print(f"\nJSON_OUTPUT_START", flush=True)
        print(json.dumps(output), flush=True)
        print(f"JSON_OUTPUT_END", flush=True)
        
    except Exception as e:
        print(f"FATAL_ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
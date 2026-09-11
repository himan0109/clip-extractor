#!/usr/bin/env python3
"""
PostgreSQL-only database module for maintaining video records.

This repo intentionally does NOT support any JSON database fallback.

The socket directory is managed by scripts/local_postgres.sh and exported
via DATABASE_URL in run.sh. Because the project path contains spaces and
postgres's -k flag cannot accept paths with spaces, the socket lives in
$XDG_RUNTIME_DIR/veng-postgres rather than inside the repo folder.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_REPO_ROOT = Path(__file__).resolve().parent


def _database_url() -> str:
    """
    Return DATABASE_URL or raise.

    Expected format (set by run.sh):
      postgresql:///veng?user=<user>&host=<socket_dir>&port=<port>
    The port is auto-detected and exported by run.sh — do not hardcode it.

    If DATABASE_URL is not set in the environment, auto-detect from
    .postgres-data/postmaster.pid so scripts run directly (without run.sh)
    still connect to the local Postgres instance.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        pid_file = _REPO_ROOT / ".postgres-data" / "postmaster.pid"
        if pid_file.exists():
            try:
                lines = pid_file.read_text().splitlines()
                port   = lines[3].strip()
                socket = lines[4].strip()
                user   = os.environ.get("USER") or os.environ.get("LOGNAME", "")
                url = f"postgresql:///veng?user={user}&host={socket}&port={port}"
            except Exception:
                pass
    if not url:
        raise RuntimeError("DATABASE_URL is required. Make sure to start the app via run.sh.")

    parsed = urlparse(url)
    q = parse_qs(parsed.query)

    host = None
    if "host" in q and q["host"]:
        host = q["host"][0]
    elif parsed.hostname:
        host = parsed.hostname

    if not host:
        raise RuntimeError(
            "DATABASE_URL must include a host= pointing to the Postgres socket directory."
        )

    return url


def _maybe_iso(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def _serialize_video_dict(video: dict) -> dict:
    """Return a JSON-serializable video dict (dates as ISO strings)."""
    if not isinstance(video, dict):
        return video
    video = dict(video)
    for k in ("scheduled_datetime", "created_datetime", "uploaded_datetime"):
        if k in video:
            video[k] = _maybe_iso(video.get(k))
    return video


def _pg_connect():
    """Connect to Postgres using DATABASE_URL (required)."""
    try:
        import psycopg2  # type: ignore
        return psycopg2.connect(_database_url())
    except Exception as e:
        raise RuntimeError(f"Postgres connection failed: {e}")


def _pg_ensure_schema(conn):
    """Ensure the videos table exists (idempotent)."""
    ddl = """
    CREATE TABLE IF NOT EXISTS videos (
      id BIGSERIAL PRIMARY KEY,
      title TEXT NOT NULL,
      script TEXT NULL,
      description TEXT NULL,
      platforms JSONB NOT NULL,
      scheduled_datetime TIMESTAMPTZ NULL,
      created_datetime TIMESTAMPTZ NOT NULL,
      uploaded BOOLEAN NOT NULL DEFAULT FALSE,
      uploaded_datetime TIMESTAMPTZ NULL,
      source TEXT NULL,
      platforms_uploaded_details JSONB NOT NULL DEFAULT '{}'::jsonb,
      csv_data JSONB NULL
    );
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
        cur.execute("""
            ALTER TABLE videos ADD COLUMN IF NOT EXISTS csv_data JSONB NULL;
        """)
    conn.commit()


def check_duplicate(title, script, platform_type=None, platform_id=None):
    """
    Check if a video with the same title and script was successfully uploaded to a specific platform/channel/page.
    Only checks videos that were successfully uploaded (uploaded = True).
    """
    title_lower = title.strip().lower() if title else ""
    script_lower = script.strip().lower() if script else ""

    conn = _pg_connect()
    try:
        _pg_ensure_schema(conn)
        with conn.cursor() as cur:
            base_sql = """
              SELECT
                id, title, script, description, platforms,
                scheduled_datetime, created_datetime, uploaded, uploaded_datetime,
                source, platforms_uploaded_details
              FROM videos
              WHERE uploaded = TRUE
                AND lower(title) = %s
                AND lower(COALESCE(script, '')) = %s
            """
            params = [title_lower, script_lower]

            if platform_type and platform_id:
                if platform_type == "YouTube":
                    base_sql += " AND (platforms_uploaded_details->'youtube_channels') ? %s"
                    params.append(platform_id)
                elif platform_type == "Instagram":
                    base_sql += " AND (platforms_uploaded_details->'instagram_accounts') ? %s"
                    params.append(platform_id)
                elif platform_type == "Facebook":
                    base_sql += " AND (platforms_uploaded_details->'facebook_pages') ? %s"
                    params.append(platform_id)
                elif platform_type == "BrowserReel":
                    base_sql += " AND (platforms_uploaded_details->'browser_reel_accounts') ? %s"
                    params.append(platform_id)

            base_sql += " ORDER BY id DESC LIMIT 1"
            cur.execute(base_sql, params)
            row = cur.fetchone()
            if not row:
                return None

            colnames = [d[0] for d in cur.description]
            video = dict(zip(colnames, row))
            return _serialize_video_dict(video)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def add_video_record(title, script, description, platforms, scheduled_datetime, created_datetime=None, source=None, csv_data=None):
    """Add a new video record to the database. source: 'automation' | 'clipout_shorts'."""
    if created_datetime is None:
        created_datetime = datetime.now(timezone.utc).isoformat()

    try:
        import psycopg2.extras  # type: ignore
        conn = _pg_connect()
        _pg_ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos
                  (title, script, description, platforms, scheduled_datetime, created_datetime,
                   uploaded, uploaded_datetime, source, platforms_uploaded_details, csv_data)
                VALUES
                  (%s, %s, %s, %s::jsonb, %s, %s,
                   FALSE, NULL, %s, %s::jsonb, %s::jsonb)
                RETURNING id
                """,
                (
                    title,
                    script,
                    description,
                    psycopg2.extras.Json(platforms if platforms is not None else []),
                    scheduled_datetime,
                    created_datetime,
                    source,
                    psycopg2.extras.Json(
                        {"youtube_channels": [], "instagram_accounts": [], "facebook_pages": [], "browser_reel_accounts": []}
                    ),
                    psycopg2.extras.Json(csv_data) if csv_data is not None else None,
                ),
            )
            _ = cur.fetchone()
        conn.commit()
        return True
    except Exception as e:
        print(f"Error saving to Postgres: {e}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_video_uploaded(title, script, platforms_uploaded_details):
    """
    Mark a video as uploaded with specific platform/channel/page details,
    then automatically sync the post to Notion (first upload only).
    """
    title_lower = title.strip().lower() if title else ""
    script_lower = script.strip().lower() if script else ""

    _notion_source = None
    _notion_details = None
    _already_synced = False
    success = False

    try:
        import psycopg2.extras  # type: ignore
        conn = _pg_connect()
        _pg_ensure_schema(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, platforms_uploaded_details, source,
                       script, scheduled_datetime
                FROM videos
                WHERE lower(title) = %s
                  AND lower(COALESCE(script, '')) = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (title_lower, script_lower),
            )
            row = cur.fetchone()
            if not row:
                return False

            vid = row["id"]
            existing = row.get("platforms_uploaded_details") or {}
            _already_synced = bool(existing.get("notion_page_id"))
            _notion_source = row.get("source") or ""
            _notion_script = row.get("script") or script or ""
            _scheduled_dt  = row.get("scheduled_datetime")

            existing.setdefault("youtube_channels", [])
            existing.setdefault("instagram_accounts", [])
            existing.setdefault("facebook_pages", [])
            existing.setdefault("browser_reel_accounts", [])
            existing.setdefault("youtube_channel_names", {})
            existing.setdefault("instagram_account_names", {})
            existing.setdefault("facebook_page_names", {})

            incoming = platforms_uploaded_details or {}

            def _merge_list(key):
                existing_list = set(existing.get(key, []) or [])
                incoming_list = set(incoming.get(key, []) or [])
                existing[key] = list(existing_list | incoming_list)

            def _merge_dict(key):
                existing.setdefault(key, {})
                if isinstance(incoming.get(key), dict):
                    existing[key].update(incoming.get(key))

            _merge_list("youtube_channels")
            _merge_list("instagram_accounts")
            _merge_list("facebook_pages")
            _merge_list("browser_reel_accounts")
            _merge_dict("youtube_channel_names")
            _merge_dict("instagram_account_names")
            _merge_dict("facebook_page_names")

            # Pass-through keys — not merged but must reach Notion sync
            for _k in ("carousel_local_paths", "notion_caption", "notion_hashtags",
                       "video_drive_url", "uploaded_file_url",
                       "uploaded_file_urls", "uploaded_file_name"):
                if _k in incoming:
                    existing[_k] = incoming[_k]

            cur.execute(
                """
                UPDATE videos
                SET uploaded = TRUE,
                    uploaded_datetime = NOW(),
                    platforms_uploaded_details = %s::jsonb
                WHERE id = %s
                """,
                (psycopg2.extras.Json(existing), vid),
            )
        conn.commit()
        _notion_details   = dict(existing)
        success = True
    except Exception as e:
        print(f"Error updating Postgres: {e}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Notion sync — runs after DB connection is closed, so it doesn't hold the connection.
    if success and not _already_synced:
        try:
            from notion_sync import sync_post_to_notion  # type: ignore
            page_id = sync_post_to_notion(
                title=title,
                script=_notion_script,
                source=_notion_source,
                platforms_uploaded_details=_notion_details,
                scheduled_datetime=_scheduled_dt,
                uploaded_datetime=datetime.now(timezone.utc),
            )
            if page_id:
                update_platforms_details(title, script, {"notion_page_id": page_id})
        except Exception as e:
            print(f"[Notion] Sync error (upload still succeeded): {e}", file=sys.stderr)

    return success


def update_platforms_details(title, script, details_update):
    """
    Merge details_update into a record's platforms_uploaded_details WITHOUT changing the
    uploaded flag.  Used to record pending/scheduled platform state before the post goes live.
    Lists are merged via union; dicts are shallow-merged; scalar values are overwritten.
    """
    title_lower = title.strip().lower() if title else ""
    script_lower = script.strip().lower() if script else ""

    try:
        import psycopg2.extras  # type: ignore
        conn = _pg_connect()
        _pg_ensure_schema(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, platforms_uploaded_details
                FROM videos
                WHERE lower(title) = %s
                  AND lower(COALESCE(script, '')) = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (title_lower, script_lower),
            )
            row = cur.fetchone()
            if not row:
                return False

            vid = row["id"]
            existing = row.get("platforms_uploaded_details") or {}

            for key, val in (details_update or {}).items():
                if isinstance(val, list):
                    existing_list = set(existing.get(key, []) or [])
                    existing[key] = list(existing_list | set(val))
                elif isinstance(val, dict):
                    existing.setdefault(key, {})
                    existing[key].update(val)
                else:
                    existing[key] = val

            cur.execute(
                """
                UPDATE videos
                SET platforms_uploaded_details = %s::jsonb
                WHERE id = %s
                """,
                (psycopg2.extras.Json(existing), vid),
            )
        conn.commit()
        return True
    except Exception as e:
        print(f"Error updating platforms details: {e}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_videos_by_month(year, month):
    """Get all videos created in a specific month."""
    try:
        import psycopg2.extras  # type: ignore
        conn = _pg_connect()
        _pg_ensure_schema(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  id, title, script, description, platforms,
                  scheduled_datetime, created_datetime, uploaded, uploaded_datetime,
                  source, platforms_uploaded_details
                FROM videos
                WHERE EXTRACT(YEAR FROM created_datetime) = %s
                  AND EXTRACT(MONTH FROM created_datetime) = %s
                ORDER BY created_datetime DESC
                """,
                (int(year), int(month)),
            )
            rows = cur.fetchall() or []
            return [_serialize_video_dict(dict(r)) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_all_videos():
    """Get all videos from database."""
    try:
        import psycopg2.extras  # type: ignore
        conn = _pg_connect()
        _pg_ensure_schema(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  id, title, script, description, platforms,
                  scheduled_datetime, created_datetime, uploaded, uploaded_datetime,
                  source, platforms_uploaded_details
                FROM videos
                ORDER BY created_datetime DESC
                """
            )
            rows = cur.fetchall() or []
            return [_serialize_video_dict(dict(r)) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def export_to_csv(output_file="video_database_export.csv"):
    """Export database to CSV file."""
    import csv

    videos = get_all_videos()
    if not videos:
        return False

    try:
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "ID",
                    "Source",
                    "Title",
                    "Script",
                    "Description",
                    "Platforms",
                    "Scheduled DateTime",
                    "Created DateTime",
                    "Uploaded",
                    "Uploaded DateTime",
                    "YouTube Channels",
                    "Instagram Accounts",
                    "Facebook Pages",
                ]
            )

            for video in videos:
                platforms_uploaded_details = video.get("platforms_uploaded_details", {}) or {}
                youtube_channels = ", ".join(platforms_uploaded_details.get("youtube_channels", []) or [])
                instagram_accounts = ", ".join(platforms_uploaded_details.get("instagram_accounts", []) or [])
                facebook_pages = ", ".join(platforms_uploaded_details.get("facebook_pages", []) or [])

                source = video.get("source") or ""
                if source == "automation":
                    source = "Automation"
                elif source == "clipout_shorts":
                    source = "Clipout Shorts"

                writer.writerow(
                    [
                        video.get("id", ""),
                        source,
                        video.get("title", ""),
                        video.get("script", ""),
                        video.get("description", ""),
                        ", ".join(video.get("platforms", []) or []),
                        video.get("scheduled_datetime", ""),
                        video.get("created_datetime", ""),
                        "Yes" if video.get("uploaded", False) else "No",
                        video.get("uploaded_datetime", ""),
                        youtube_channels,
                        instagram_accounts,
                        facebook_pages,
                    ]
                )
        return True
    except Exception as e:
        print(f"Error exporting to CSV: {e}", file=sys.stderr)
        return False


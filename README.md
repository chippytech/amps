<div style="
  display: flex;
  justify-content: center;
">
  <img src="/assets/logo.svg" height="140" alt="The logo with a dark blue lightning bolt, combined with the text in the Rubik font saying Amps.">
</div>
<hr>


**Amps** is a Flask-based server that dynamically generates and serves `.m3u` playlists, relays or transcodes media streams using **FFmpeg**, and allows for easy configuration through a `config.yaml` file.

It's designed to be a developer-friendly, modular, and robust solution for personal or small-scale media streaming needs.



## Features

- **Dynamic M3U Playlists:** Serves a `/playlist.m3u` file compatible with most media players (VLC, Kodi, etc.) complete with channel names, logos, and custom metadata.
- **FFmpeg Engine:** Relays streams (`copy` codec) or transcodes them on-the-fly to different bitrates, resolutions, or formats.
- **Multi-Protocol Outputs:** Generate HLS (and LL-HLS), MPEG-DASH, RTSP, or raw TS outputs from the same FFmpeg process.
- **Static Manifests:** HLS playlists and DASH manifests are written to a temp media directory and served via `/hls/<id>/index.m3u8` and `/dash/<id>/manifest.mpd`.
- **yt-dlp Integration:** Resolve complex streaming services (YouTube, Twitch, etc.) into direct FFmpeg inputs on demand.
- **YAML Configuration:** All streams and FFmpeg profiles are defined in a simple, human-readable `config.yaml`.
- **REST API:** A simple API to list, add, update, and delete streams in-memory without restarting the server.
- **XMLTV + EPG Feed:** Export upcoming programs as XMLTV via `/epg.xml` or JSON through `/api/epg` for guide ingestion.
- **Adaptive Bitrate Variants:** Configure per-channel variants that swap FFmpeg profiles on demand for low/high bitrate viewers.
- **Region Locking:** Allow or block specific ISO country codes per channel and automatically filter playlists by region.
- **Upcoming Programming:** Attach next-up schedules to channels and retrieve them through the API for live program feeds.
- **Custom FFmpeg Pipelines:** Override the FFmpeg command per channel when you need full control over how the stream is produced.
- **Protocol-Friendly Inputs:** Add FFmpeg input options to unlock RTMP, DVB/IP, DTV, multicast and other specialised transports.
- **Audio-Only Endpoints:** Expose lightweight AAC outputs at `/audio/<id>` for radio or podcast-style listening.
- **Token Authentication:** Secure your streams with a shared token, passed via headers or URL parameters.
- **Robust Process Management:** Automatically restarts broken streams on request and gracefully cleans up FFmpeg processes on shutdown.
- **Containerized:** Includes a `Dockerfile` for easy, production-ready deployment.
- **CLI Interface:** Manage the server with simple commands like `amps serve` and `amps list`.
- **Scheduled Streams:** Define time-bound channels that automatically activate and retire using APScheduler.

## Architecture

Amps consists of several key components:

1.  **Flask Server (`server.py`):** The web core that handles HTTP requests.
    -   `/playlist.m3u`: Generates the playlist from the current stream configuration.
    -   `/stream/<id>`: Serves the video content. This is a streaming endpoint that holds a connection open.
    -   `/api/streams`: Provides CRUD operations for in-memory stream management.
    -   `/metrics`: Exposes basic server metrics.
2.  **FFmpeg Wrapper (`ffmpeg_utils.py`):** Manages all FFmpeg subprocesses. It starts, stops, and monitors processes, ensuring that a stream is available when requested. It uses `ffmpeg-python` to build FFmpeg commands safely.
3.  **Configuration (`config_loader.py`):** Parses and validates `config.yaml` at startup.
4.  **CLI (`cli.py`):** Provides command-line entry points using `click`. When `server.debug` is `false`, it launches a production-ready `gunicorn` server.

## Setup and Installation

### 1. Prerequisites

-   Python 3.7+
-   `ffmpeg` installed and available in your system's PATH.
-   [Docker](https://www.docker.com/get-started) (for containerized deployment).

### 2. Local Installation (for development)

1.  **Clone the repository:**
    ```bash
    git clone <repository_url>
    cd amps-project
    ```
2.  **Create a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Configure `config.yaml`:**
    Modify the `config.yaml` file to add your streams, scheduled stream windows, and set a secure auth token. Channels can now specify additional metadata such as logos, alternate guide names, upcoming programs, and even bespoke FFmpeg commands.

5.  **Run the server:**
    ```bash
    python -m amps serve
    ```

### 3. Docker Deployment (recommended for production)

1.  **Configure `config.yaml`:**
    Make sure your `config.yaml` is ready.

2.  **Build the Docker image:**
    ```bash
    docker build -t amps-server .
    ```

3.  **Run the container:**
    ```bash
    docker run -d -p 5000:5000 --name amps -v $(pwd)/config.yaml:/app/config.yaml --restart unless-stopped amps-server
    ```
    This command:
    -   Runs the container in detached mode (`-d`).
    -   Maps port 5000 on your host to port 5000 in the container (`-p`).
    -   Mounts your local `config.yaml` into the container, allowing you to change it without rebuilding the image (`-v`).
    -   Ensures the container automatically restarts if it stops (`--restart unless-stopped`).

## Usage

### Accessing the Playlist

Open this URL in a browser or media player like VLC. You must provide the token.

**URL Format:**
`http://<server_ip>:5000/playlist.m3u?token=<your_token>`

**cURL Example:**
```bash
curl -L "http://localhost:5000/playlist.m3u?token=changeme123"
```

The playlist includes extended metadata when available:

- `tvg-name`, `tvg-logo`, `group-title`, and `channel-number` attributes.
- `#EXTREM:AMP-NEXT` lines for the next scheduled program (title, start time, description).
- `#EXTREM:AMP-PROGRAM-FEED` linking to external schedule feeds.
- `#EXTREM:AMP-DESCRIPTION` containing rich channel descriptions.
- `#EXTREM:AMP-VARIANT` entries that describe the adaptive bitrate rendition served by the URL.
- `#EXTREM:AMP-REGION` hints showing the allow/block lists applied to the channel.

Players that ignore custom tags will safely skip them, while companion applications can parse them for richer experiences.

#### Dynamic filters

`/playlist.m3u` now responds to optional query parameters so you can build targeted line-ups:

| Parameter | Description |
| --- | --- |
| `region` | Two-letter ISO code (`US`, `GB`, ...). Also accepted via headers `X-Amps-Region`, `CF-IPCountry`, or `X-Region`. Streams are hidden if the viewer is outside their allow-list. |
| `group` | Comma-separated group titles. Only matching channels remain. |
| `ids` | Comma-separated channel IDs for ad-hoc playlists. |
| `variants` | Set to `false` to hide adaptive bitrate variants from the playlist. |

The generated stream URLs already include the auth token and the `region` code so media players can pass the same restrictions to `/stream/<id>`.

### Static HLS/DASH Manifests

When a stream uses an HLS or DASH output profile, manifests and media segments are written to a temporary directory (by default under your OS temp root). They are automatically served by Flask using the following routes:

- `http://<server_ip>:5000/hls/<id>/index.m3u8` (LL-HLS uses the same path with different flags)
- `http://<server_ip>:5000/dash/<id>/manifest.mpd`

The base temp directory can be overridden via `media_root` in `config.yaml`. Each stream/variant gets its own subfolder, allowing multiple clients to reuse the same FFmpeg process.

### Audio-Only Output

Every stream can also be listened to in audio-only form. The `/audio/<id>` endpoint starts (or reuses) an FFmpeg process that strips the video track and outputs AAC by default. This is useful for low-bandwidth scenarios or podcast-style playback.

### XMLTV + EPG feeds

- `/epg.xml` emits an XMLTV feed derived from every channel's `next_programs` list.
- `/api/epg` returns the same guide data in JSON for dashboards.

Both endpoints understand the same `region`, `group`, and `ids` filters as the playlist route, allowing per-market exports.

## Stream Configuration Reference

Each entry in the `streams` list accepts the following keys:

| Key | Required | Description |
| --- | --- | --- |
| `id` | ✅ | Unique integer identifier for the stream. |
| `name` | ✅ | Display name for the channel. |
| `source` | ✅ | Input URL or file path passed to FFmpeg. |
| `ffmpeg_profile` | ✅* | Name of a profile defined under `ffmpeg_profiles`. Required unless `custom_ffmpeg` is supplied. |
| `custom_ffmpeg` | ✅* | Optional override to launch a bespoke FFmpeg command instead of a profile. Accepts a string or mapping with `command`, `shell`, `cwd`, and `env` fields. The `{source}`, `{id}`, and `{name}` placeholders are expanded. |
| `tvg_name` | | Alternate guide name used in playlist metadata. |
| `epg_id` | | Override the XMLTV/playlist identifier (defaults to the numeric `id`). |
| `logo` | | URL to the channel logo. |
| `group` | | Logical group title for player UIs. |
| `channel_number` | | Numeric channel identifier for compatible players. |
| `description` | | Long-form description inserted as a custom playlist tag. |
| `program_feed` | | URL to an external schedule feed for companion apps. |
| `next_programs` | | List of upcoming program objects with at least a `title`, plus optional `start` and `description` fields. |
| `regions_allowed` | | List of ISO country codes that are allowed to view the stream. |
| `regions_blocked` | | List of ISO country codes that are denied while the rest remain accessible. |
| `adaptive_bitrates` | | List of variant definitions for adaptive bitrate playback. |
| `use_yt_dlp` | | Convenience flag to resolve `source` with yt-dlp before launching FFmpeg. |
| `yt_dlp_format` | | Override the yt-dlp format selector (defaults to `best`). |
| `source_handler` | | Advanced yt-dlp configuration mapping (`type: yt_dlp`, optional `format`, `options`, ...). |
| `input_options` | | Mapping of keyword arguments passed to `ffmpeg.input` (e.g. `protocol_whitelist`, `rtmp_live`). |
| `input_args` | | Additional positional arguments for `ffmpeg.input` enabling flags such as `-stream_loop`. |
| `output_format` | | Target container for the stream (`ts`, `hls`, `ll-hls`, `dash`, `rtsp`, or `audio`). Defaults to MPEG-TS. |
| `ll_hls` | | When `true`, enables low-latency HLS flags for HLS outputs. |
| `audio_only` | | Forces the FFmpeg output to drop the video track and encode audio (defaults to AAC). Used by `/audio/<id>`. |
| `hwaccel` | | Optional hardware acceleration block (e.g., `type: nvidia`, `device: 0`). Adds `-hwaccel`/`-hwaccel_device` flags. |

> ℹ️ Provide either `ffmpeg_profile` or `custom_ffmpeg`. When both are supplied, the profile is still available for reference but the custom command takes precedence.

### Advanced Input Handling

- `use_yt_dlp` / `source_handler`: Automatically resolve the `source` URL through [yt-dlp](https://github.com/yt-dlp/yt-dlp) so you can restream providers like YouTube, Twitch, Facebook Live, and more. The resolved URL and headers are refreshed every time a new FFmpeg process starts.
- `input_options` / `input_args`: Fine-tune how FFmpeg opens the stream, enabling RTMP authentication (`rtmp_conn`, `rtmp_app`), IPTV transports (`protocol_whitelist: "file,udp,rtp,tcp,tls,http,https,rtmp,rtsp"`), satellite/digital TV inputs, multicast, etc.

#### yt-dlp Example

```yaml
- id: 10
  name: "YouTube Live Demo"
  source: "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
  ffmpeg_profile: copy
  use_yt_dlp: true
  input_options:
    protocol_whitelist: "file,http,https,tcp,tls,crypto"
  output_format: hls
```

For more control, switch to the richer `source_handler` form:

```yaml
- id: 11
  name: "Twitch with yt-dlp"
  source: "https://www.twitch.tv/nasa"
  ffmpeg_profile: copy
  source_handler:
    type: yt_dlp
    format: "best"
    options:
      live_from_start: true
      retries: 3
  input_options:
    protocol_whitelist: "file,http,https,tcp,tls,crypto"
```

Use `input_args` for flags that do not take values as key-value pairs:

```yaml
- id: 12
  name: "RTMP ingest"
  source: "rtmp://example.com/live/feed"
  ffmpeg_profile: copy
  input_options:
    rtmp_live: live
  input_args:
    - "-re"
```

These controls make it straightforward to connect to RTMP, DVB-IP, DTV, or other specialised transports without falling back to a full custom command.

### Adaptive bitrate variants

Define variants under `adaptive_bitrates` to expose alternate FFmpeg profiles (for example, a low bitrate copy for mobile viewers). Every variant requires a unique `name` and can optionally include `label`, `ffmpeg_profile`, `custom_ffmpeg`, `source`, `input_options`, or `input_args` overrides. The playlist will automatically append entries such as `Channel Name (SD)` that point to `/stream/<id>?variant=sd`.

Clients that prefer a specific rendition can either play the `variant` entry from the playlist or add `?variant=<name>` when calling the `/stream/<id>` endpoint directly.

### Region locking

Set `regions_allowed` or `regions_blocked` on any stream to restrict playback. The server inspects the `region` query parameter first, then a few common CDN headers (`X-Amps-Region`, `X-Region`, `CF-IPCountry`, `X-Appengine-Country`). If a viewer's region does not satisfy the constraints, both the playlist and `/stream/<id>` return a 403 response for that channel.

### Scheduled Streams

Use the optional `scheduled_streams` section in your configuration to create channels that appear only inside specific windows. The server relies on [APScheduler](https://apscheduler.readthedocs.io/en/3.x/) to activate and deactivate these channels without restarts.

Example configuration snippet:

```yaml
scheduled_streams:
  - id: 900
    name: "Community Spotlight"
    ffmpeg_profile: hls-transcode
    source: https://example.com/community_spotlight.m3u8
    schedule:
      start: "2024-05-04T18:00:00Z"
      end: "2024-05-04T20:00:00Z"
```

- `start` and `end` accept ISO-8601 timestamps (`YYYY-MM-DDTHH:MM:SSZ`).
- When the current time passes the `start`, the stream is added to the in-memory map just like static entries.
- When the `end` time is reached, the stream is removed and any running FFmpeg process is stopped.
- If `start` is omitted or in the past, the stream activates immediately; omit `end` to keep the channel active indefinitely.
- Scheduled streams cannot reuse IDs from the always-on `streams` section—duplicates are ignored with a warning.

### Managing Upcoming Programs via the API

Use the new endpoint to fetch or replace the upcoming schedule for a channel without reloading configuration files:

```
GET  /api/streams/<id>/programs
PUT  /api/streams/<id>/programs
```

The `PUT` body should be a JSON array of program objects. Each object must contain a `title` and can optionally include `start` and `description` fields.

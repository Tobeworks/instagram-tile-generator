# insta-tile-generator

Generate Instagram-ready MP4 tiles from audio tracks and cover art. Each tile combines a full-bleed cover image, an animated waveform, and a text overlay (headline, track title, optional copy). Output is exported as MP4 (feed or Reels format) or as static PNGs for carousel posts.

## Features

- Animated waveform derived from the audio signal
- Full-bleed cover image with smooth dark gradient overlay
- Three output formats: square feed (1:1), Reels/Stories (9:16), carousel (static PNGs)
- Clip taken from the center of the track by default
- Per-track or global start offset, specified as a timecode (`1:23`) or seconds (`83`)
- Audio fade-in and fade-out
- Config file auto-generation via `--init`
- Falls back to filename-based metadata if no `config.json` is present

## Requirements

Python 3.10+ and [ffmpeg](https://ffmpeg.org/) installed on your system.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Data layout

```
data/
└── my-release/
    ├── cover.png               # shared cover image (fallback for all tracks)
    ├── track-01.wav
    ├── track-02.wav
    └── config.json             # optional — see below
export/
└── my-release/
    ├── track_01.mp4
    ├── track_02.mp4
    └── carousel/
        ├── 01_track_01.png
        └── 02_track_02.png
```

Multiple audio formats are supported as long as ffmpeg can decode them (`.wav`, `.mp3`, `.flac`, `.aiff`, etc.). Only `.wav` files are discovered automatically when no `config.json` is present.

## Quickstart

```bash
# Generate a config.json skeleton from existing files, then edit it
python generate.py data/my-release --init

# Render square feed tiles (1080×1080), 30s clips
python generate.py data/my-release --duration 30

# Check the output visually before a full render
python generate.py data/my-release --preview
```

## CLI reference

```
python generate.py <data_dir> [options]
```

| Argument | Description |
|---|---|
| `data_dir` | Path to the release folder, e.g. `data/my-release` |
| `--init` | Scan `data_dir` and write a `config.json` skeleton, then exit |
| `--duration SECS` | Clip length in seconds. Overrides `config.json`. |
| `--start TIME` | Clip start time as `M:SS` or seconds, e.g. `1:23` or `83`. Overrides `config.json` per-track values. Default: center of track. |
| `--format FORMAT` | `square` (1080×1080, default), `reel` (1080×1920), `carousel` (static PNGs) |
| `--preview` | Render a static PNG preview per track instead of encoding MP4 |

### `--init` with a custom duration

`--duration` is respected by `--init` to pre-calculate the center offset for each track:

```bash
python generate.py data/my-release --init --duration 45
```

## config.json

```json
{
  "ep_title": "My Release",
  "clip_duration": 30,
  "tracks": [
    {
      "audio":    "track-01.wav",
      "image":    "cover.png",
      "headline": "OUT NOW",
      "title":    "Track Title",
      "copy":     "Optional copy text shown below the title",
      "start":    "1:23"
    }
  ]
}
```

| Field | Required | Description |
|---|---|---|
| `ep_title` | no | Used as the export subfolder name |
| `clip_duration` | no | Default clip length in seconds |
| `tracks[].audio` | yes | Filename relative to the release folder |
| `tracks[].image` | no | Cover image filename. Falls back to `cover.png` |
| `tracks[].headline` | no | Small uppercase label above the title |
| `tracks[].title` | no | Track title. Parsed from filename if omitted |
| `tracks[].copy` | no | Small text line below the title |
| `tracks[].start` | no | Clip start as `M:SS` or seconds. Default: center of track |

## Output

Rendered files are written to `./export/<release-name>/`.

| Format | Path | Resolution |
|---|---|---|
| `square` | `export/<release>/track.mp4` | 1080×1080 |
| `reel` | `export/<release>/track.mp4` | 1080×1920 |
| `carousel` | `export/<release>/carousel/01_track.png` | 1080×1080 |
| `--preview` | `export/<release>/track_preview.png` | format-dependent |

The 1080×1080 canvas uses 160 px of horizontal padding, keeping all content within the safe zone that remains visible when Instagram crops to 4:5.

#!/usr/bin/env python3
"""
insta-tile-generator — turn audio + cover art into Instagram-ready MP4 tiles.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import librosa
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FORMATS = {
    "square":   (1080, 1080),
    "reel":     (1080, 1920),
    "carousel": (1080, 1080),
}

PADDING_X        = 160
FPS              = 24
N_BARS           = 60
MAX_BAR_HEIGHT   = 120
BAR_GAP          = 4
WAVEFORM_COLOR   = (255, 255, 255, 200)
OVERLAY_ALPHA    = 180


# ── Fonts ─────────────────────────────────────────────────────────────────────

def _find_font(bold=False):
    candidates = (
        [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFPro-Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ] if bold else [
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    return next((p for p in candidates if os.path.exists(p)), None)


def get_font(size, bold=False):
    path = _find_font(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_-]+", "_", text)


def parse_track_title(filename):
    stem = Path(filename).stem
    if " - " in stem:
        stem = stem.split(" - ", 1)[1]
    return re.sub(r"_\d+$", "", stem).strip()


def parse_timecode(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip()
    if ":" in value:
        parts = value.split(":")
        return sum(float(p) * 60 ** (len(parts) - 1 - i) for i, p in enumerate(parts))
    return float(value)


def format_timecode(seconds):
    m = int(seconds) // 60
    s = seconds - m * 60
    return f"{m}:{s:05.2f}"


def get_audio_duration(audio_path: Path) -> float:
    import soundfile as sf
    return sf.info(str(audio_path)).duration


def resolve_start_time(audio_path: Path, clip_duration, explicit_start) -> float:
    if explicit_start is not None:
        return max(0.0, explicit_start)
    total = get_audio_duration(audio_path)
    if clip_duration and clip_duration < total:
        return max(0.0, (total - clip_duration) / 2)
    return 0.0


def _track_dict(audio, image, headline="", title=None, copy="", start=None):
    return {
        "audio":    audio,
        "image":    image,
        "headline": headline,
        "title":    title or parse_track_title(audio.name if hasattr(audio, "name") else str(audio)),
        "copy":     copy,
        "start":    parse_timecode(start),
    }


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(data_dir: Path):
    config_path = data_dir / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        ep_name = slugify(cfg.get("ep_title", data_dir.name))
        clip_duration = cfg.get("clip_duration", None)
        tracks = [
            _track_dict(
                audio    = data_dir / t["audio"],
                image    = data_dir / t.get("image", "cover.png"),
                headline = t.get("headline", ""),
                title    = t.get("title", parse_track_title(t["audio"])),
                copy     = t.get("copy", ""),
                start    = t.get("start", None),
            )
            for t in cfg.get("tracks", [])
        ]
        return ep_name, clip_duration, tracks

    ep_name = slugify(data_dir.name)
    covers = list(data_dir.glob("cover.*"))
    cover = covers[0] if covers else None

    wav_files = sorted(data_dir.glob("*.wav"))
    if not wav_files:
        print(f"No WAV files found in {data_dir}", file=sys.stderr)
        sys.exit(1)

    tracks = [_track_dict(audio=wav, image=cover) for wav in wav_files]
    return ep_name, None, tracks


def generate_config(data_dir: Path, clip_duration_default=30):
    config_path = data_dir / "config.json"
    if config_path.exists():
        print(f"config.json already exists at {config_path}\nDelete it first or edit it directly.")
        sys.exit(1)

    covers = list(data_dir.glob("cover.*"))
    cover_name = covers[0].name if covers else "cover.png"

    wav_files = sorted(data_dir.glob("*.wav"))
    if not wav_files:
        print(f"No WAV files found in {data_dir}", file=sys.stderr)
        sys.exit(1)

    tracks = []
    for wav in wav_files:
        total = get_audio_duration(wav)
        auto_start = max(0.0, (total - clip_duration_default) / 2)
        tracks.append({
            "audio":    wav.name,
            "image":    cover_name,
            "headline": "",
            "title":    parse_track_title(wav.name),
            "copy":     "",
            "start":    format_timecode(auto_start),
        })
        print(f"  {wav.name}: {total:.1f}s  →  start={format_timecode(auto_start)}")

    cfg = {
        "ep_title":      data_dir.name.replace("-", " ").title(),
        "clip_duration": clip_duration_default,
        "tracks":        tracks,
    }
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    print(f"\nconfig.json written to {config_path}")
    print("Edit headline, title, copy, and start offsets as needed.")


# ── Waveform ──────────────────────────────────────────────────────────────────

def extract_waveform_frames(audio_path: Path, offset, duration, fps, n_bars):
    y, sr = librosa.load(str(audio_path), sr=None, mono=True, offset=offset or 0.0, duration=duration)
    total_samples = len(y)
    n_frames = int((total_samples / sr) * fps)
    if n_frames == 0:
        return np.zeros((1, n_bars))

    frames = np.zeros((n_frames, n_bars))
    samples_per_frame = total_samples / n_frames
    samples_per_bar = samples_per_frame / n_bars

    for fi in range(n_frames):
        frame_start = int(fi * samples_per_frame)
        frame_audio = y[frame_start:frame_start + int(samples_per_frame)]
        if len(frame_audio) == 0:
            continue
        for bi in range(n_bars):
            segment = frame_audio[int(bi * samples_per_bar):int((bi + 1) * samples_per_bar)]
            if len(segment) > 0:
                frames[fi, bi] = np.sqrt(np.mean(segment ** 2))

    max_val = frames.max()
    if max_val > 0:
        frames /= max_val
    return frames


# ── Renderer ──────────────────────────────────────────────────────────────────

def _crop_and_resize(img: Image.Image, w: int, h: int) -> Image.Image:
    target_ratio = w / h
    if img.width / img.height > target_ratio:
        new_w = int(img.height * target_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / target_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))
    return img.resize((w, h), Image.LANCZOS)


def render_frame(image_path, bar_heights, text_config, size):
    w, h = size

    if image_path and Path(image_path).exists():
        img = _crop_and_resize(Image.open(image_path).convert("RGB"), w, h)
    else:
        img = Image.new("RGB", (w, h), (20, 20, 30))

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    gradient_top = int(h * 0.25)
    for y_pos in range(gradient_top, h):
        progress = (y_pos - gradient_top) / (h - gradient_top)
        draw_ov.line([(0, y_pos), (w, y_pos)], fill=(0, 0, 0, int(OVERLAY_ALPHA * (progress ** 2.8))))

    frame = Image.alpha_composite(img.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(frame)

    if bar_heights is not None and len(bar_heights) > 0:
        bar_w = max(2, (w - 2 * PADDING_X - (N_BARS - 1) * BAR_GAP) // N_BARS)
        total_bar_w = N_BARS * bar_w + (N_BARS - 1) * BAR_GAP
        x0 = (w - total_bar_w) // 2
        cy = int(h * 0.52)
        for i, height_norm in enumerate(bar_heights):
            bh = max(3, int(height_norm * MAX_BAR_HEIGHT))
            x = x0 + i * (bar_w + BAR_GAP)
            draw.rectangle([x, cy - bh, x + bar_w, cy + bh], fill=WAVEFORM_COLOR)

    font_headline = get_font(22)
    font_title    = get_font(58, bold=True)
    font_copy     = get_font(26)

    blocks = []
    if text_config.get("headline"):
        blocks.append(("headline", text_config["headline"], font_headline, 22))
    if text_config.get("title"):
        blocks.append(("title",    text_config["title"],    font_title,    58))
    if text_config.get("copy"):
        blocks.append(("copy",     text_config["copy"],     font_copy,     26))

    total_text_h = sum(fs for _, _, _, fs in blocks) + 12 * (len(blocks) - 1)
    text_y = h - 80 - total_text_h

    colors = {"headline": (200, 200, 200, 220), "title": (255, 255, 255, 255), "copy": (180, 180, 180, 200)}
    for kind, text, font, _ in blocks:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = max(PADDING_X, (w - (bbox[2] - bbox[0])) // 2)
        draw.text((x, text_y), text.upper() if kind == "headline" else text, font=font, fill=colors[kind])
        text_y += draw.textbbox((0, 0), text, font=font)[3] + 12

    return frame.convert("RGB")


# ── Output ────────────────────────────────────────────────────────────────────

def save_preview(track, export_dir: Path, fmt: str):
    size = FORMATS["square" if fmt == "carousel" else fmt]
    bars = np.full(N_BARS, 0.4)
    bars[N_BARS // 4: 3 * N_BARS // 4] = 0.8
    out_path = export_dir / f"{slugify(track['title'])}_preview.png"
    render_frame(track["image"], bars, track, size).save(out_path)
    print(f"  Preview saved: {out_path}")


def save_carousel_slide(track, index, carousel_dir: Path):
    out_path = carousel_dir / f"{index:02d}_{slugify(track['title'])}.png"
    render_frame(track["image"], None, track, FORMATS["carousel"]).save(out_path)
    print(f"  Carousel slide saved: {out_path}")


def create_video(track, export_dir: Path, fmt: str, clip_duration, explicit_start):
    from moviepy import VideoClip, AudioFileClip
    from moviepy.audio.fx import AudioFadeIn, AudioFadeOut  # type: ignore

    size = FORMATS[fmt]
    audio_path = track["audio"]
    start_time = resolve_start_time(
        audio_path, clip_duration,
        explicit_start if explicit_start is not None else track.get("start"),
    )

    print(f"  Extracting waveform from {audio_path.name} (start={start_time:.1f}s)...")
    waveform_frames = extract_waveform_frames(audio_path, start_time, clip_duration, FPS, N_BARS)
    n_frames = len(waveform_frames)
    actual_duration = n_frames / FPS

    def make_frame(t):
        fi = min(int(t * FPS), n_frames - 1)
        return np.array(render_frame(track["image"], waveform_frames[fi], track, size))

    print(f"  Rendering {actual_duration:.1f}s at {FPS}fps ({n_frames} frames)...")
    video = VideoClip(make_frame, duration=actual_duration)

    audio = AudioFileClip(str(audio_path))
    audio = audio.subclipped(start_time, min(start_time + actual_duration, audio.duration))
    audio = audio.with_effects([AudioFadeIn(1.5), AudioFadeOut(1.5)])
    video = video.with_audio(audio)

    out_path = export_dir / f"{slugify(track['title'])}.mp4"
    video.write_videofile(str(out_path), fps=FPS, codec="libx264", audio_codec="aac", logger=None)
    print(f"  Video saved: {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description="Generate Instagram MP4 tiles from audio tracks and cover art.",
    )
    parser.add_argument("data_dir",
                        help="Path to a release folder, e.g. data/my-ep")
    parser.add_argument("--init", action="store_true",
                        help="Scan data_dir and write a config.json skeleton, then exit")
    parser.add_argument("--duration", type=float, default=None,
                        help="Clip length in seconds (overrides config.json)")
    parser.add_argument("--start", type=str, default=None,
                        help="Clip start time: '1:23' or '83'. Default: center of track")
    parser.add_argument("--format", choices=["square", "reel", "carousel"], default="square",
                        help="square=1080×1080 MP4, reel=1080×1920 MP4, carousel=static PNGs (default: square)")
    parser.add_argument("--preview", action="store_true",
                        help="Render a static PNG preview per track, skip MP4 encoding")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"Error: directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    if args.init:
        print(f"\nGenerating config.json for {data_dir.name}...\n")
        generate_config(data_dir, clip_duration_default=int(args.duration or 30))
        sys.exit(0)

    ep_name, json_duration, tracks = load_config(data_dir)
    clip_duration  = args.duration or json_duration
    explicit_start = parse_timecode(args.start)

    project_root = Path(__file__).parent
    export_dir = project_root / "export" / ep_name / ("carousel" if args.format == "carousel" else "")
    export_dir = export_dir.resolve()
    export_dir.mkdir(parents=True, exist_ok=True)

    mode = "preview" if args.preview else args.format
    print(f"\n  release : {ep_name}")
    print(f"  format  : {args.format}  |  mode: {mode}")
    if clip_duration:
        print(f"  duration: {clip_duration}s")
    print(f"  tracks  : {len(tracks)}")
    print(f"  output  : {export_dir}\n")

    for i, track in enumerate(tracks, 1):
        print(f"[{i}/{len(tracks)}] {track['title']}")
        if not track["audio"] or not Path(track["audio"]).exists():
            print("  WARNING: audio file not found, skipping.")
            continue
        if args.preview:
            save_preview(track, export_dir.parent if args.format == "carousel" else export_dir, args.format)
        elif args.format == "carousel":
            save_carousel_slide(track, i, export_dir)
        else:
            create_video(track, export_dir, args.format, clip_duration, explicit_start)

    print("\nDone.")


if __name__ == "__main__":
    main()

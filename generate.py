"""
insta-tile-generator — turn audio + cover art into Instagram-ready MP4 tiles.
"""

import argparse
import colorsys
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

PADDING_X       = 160
FPS             = 24
N_BARS          = 60
MAX_BAR_HEIGHT  = 120
BAR_GAP         = 4
OVERLAY_ALPHA_DEFAULT   = 180  # ~0.7 opacity
TW_CHARS_PER_SEC  = 20         # typewriter speed (chars/s)
TW_FADE_DURATION  = 0.5        # fade-in duration (s) for headline / copy


# ── Fonts ─────────────────────────────────────────────────────────────────────

_FONT_SEARCH_DIRS = [
    Path.home() / "Library/Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path("/usr/share/fonts"),
]
_BOLD_HINTS    = {"bold", "black", "heavy", "semibold", "extrabold"}
_REGULAR_HINTS = {"regular", "medium", "text", "roman", "book"}


def find_font_by_name(name: str, bold: bool = False):
    """Scan system font directories for a font matching `name` (case-insensitive)."""
    import itertools
    clean = lambda s: s.lower().replace(" ", "").replace("-", "").replace("_", "")
    name_clean = clean(name)
    candidates = []
    for d in _FONT_SEARCH_DIRS:
        if d.exists():
            for f in itertools.chain(d.rglob("*.ttf"), d.rglob("*.otf")):
                if name_clean in clean(f.stem):
                    candidates.append(f)
    if not candidates:
        return None
    hints = _BOLD_HINTS if bold else _REGULAR_HINTS
    preferred = [f for f in candidates if any(h in f.stem.lower() for h in hints)]
    return str((preferred or candidates)[0])


def _find_system_font(bold=False):
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


def get_font(size, bold=False, font_path=None):
    path = font_path
    if path and not Path(path).exists():
        # treat as font name — search installed fonts
        resolved = find_font_by_name(path, bold=bold)
        if resolved:
            path = resolved
        else:
            print(f"  WARNING: font '{path}' not found — falling back to system font", file=sys.stderr)
            path = None
    path = path or _find_system_font(bold)
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


def _track_dict(audio, image, headline="", title=None, copy="", start=None,
                accent_color=None, font=None, font_color=None,
                overlay_color=None, overlay_opacity=None,
                typewriter_headline=None, typewriter_title=None, typewriter_copy=None,
                progress_bar_color=None, no_text=False):
    return {
        "audio":               audio,
        "image":               image,
        "headline":            headline,
        "title":               parse_track_title(audio.name if hasattr(audio, "name") else str(audio)) if title is None else title,
        "copy":                copy,
        "start":               parse_timecode(start),
        "accent_color":        parse_color(accent_color),
        "font":                font,
        "font_color":          parse_color(font_color),
        "overlay_color":       parse_color(overlay_color),
        "overlay_opacity":     float(overlay_opacity) if overlay_opacity is not None else None,
        "typewriter_headline": typewriter_headline,
        "typewriter_title":    typewriter_title,
        "typewriter_copy":     typewriter_copy,
        "progress_bar_color":  parse_color(progress_bar_color),
        "no_text":             bool(no_text),
    }


# ── Dominant color ────────────────────────────────────────────────────────────

def parse_color(value) -> tuple:
    """Parse a color value into an (r, g, b) tuple.
    Accepts: '#ff6b35', 'ff6b35', [255, 107, 53], or (255, 107, 53).
    Returns None if value is None.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(int(c) for c in value)
    hex_str = str(value).strip().lstrip("#")
    if len(hex_str) == 6:
        return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    raise ValueError(f"Cannot parse color: {value!r}  (use '#rrggbb' or [r, g, b])")


def get_dominant_color(image_path) -> tuple:
    """Extract the most vibrant dominant color from an image."""
    if not image_path or not Path(image_path).exists():
        return (255, 255, 255)
    img = Image.open(image_path).convert("RGB").resize((100, 100))
    quantized = img.quantize(colors=8)
    palette = quantized.getpalette()
    colors = [(palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2]) for i in range(8)]

    def vibrance(rgb):
        h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        return s * v

    r, g, b = max(colors, key=vibrance)
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    s = max(s, 0.55)
    v = max(v, 0.75)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(data_dir: Path):
    config_path = data_dir / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        ep_name = slugify(cfg.get("ep_title", data_dir.name))
        clip_duration = cfg.get("clip_duration", None)
        extras = {
            "font":                  cfg.get("font", None),
            "font_color":            parse_color(cfg.get("font_color", None)),
            "progress_bar":          cfg.get("progress_bar", True),   # default on
            "progress_bar_position": cfg.get("progress_bar_position", "top"),
            "accent_color":          parse_color(cfg.get("accent_color", None)),
            "overlay_color":         parse_color(cfg.get("overlay_color", None)),
            "overlay_opacity":       cfg.get("overlay_opacity", None),
            "format":                cfg.get("format", None),
            "waveform":              cfg.get("waveform", True),
            "typewriter_headline":   cfg.get("typewriter_headline", False),
            "typewriter_title":      cfg.get("typewriter_title", False),
            "typewriter_copy":       cfg.get("typewriter_copy", False),
            "progress_bar_color":    parse_color(cfg.get("progress_bar_color", None)),
        }
        tracks = [
            _track_dict(
                audio           = data_dir / t["audio"],
                image           = data_dir / t.get("image", "cover.png"),
                headline        = t.get("headline", ""),
                title           = t.get("title", parse_track_title(t["audio"])),
                copy            = t.get("copy", ""),
                start           = t.get("start", None),
                accent_color         = t.get("accent_color", None),
                font                 = t.get("font", None),
                font_color           = t.get("font_color", None),
                overlay_color        = t.get("overlay_color", None),
                overlay_opacity      = t.get("overlay_opacity", None),
                typewriter_headline  = t.get("typewriter_headline", None),
                typewriter_title     = t.get("typewriter_title", None),
                typewriter_copy      = t.get("typewriter_copy", None),
                progress_bar_color   = t.get("progress_bar_color", None),
                no_text              = t.get("no_text", False),
            )
            for t in cfg.get("tracks", [])
        ]
        return ep_name, clip_duration, tracks, extras

    ep_name = slugify(data_dir.name)
    covers = list(data_dir.glob("cover.*"))
    cover = covers[0] if covers else None
    wav_files = sorted(data_dir.glob("*.wav"))
    if not wav_files:
        print(f"No WAV files found in {data_dir}", file=sys.stderr)
        sys.exit(1)

    tracks = [_track_dict(audio=wav, image=cover) for wav in wav_files]
    return ep_name, None, tracks, {}


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _find_image_for_track(wav: Path, data_dir: Path):
    """Return the best matching image for a track, or None."""
    # 1. same stem as audio file
    for ext in IMAGE_EXTENSIONS:
        candidate = data_dir / (wav.stem + ext)
        if candidate.exists():
            return candidate
    # 2. cover.*
    for ext in IMAGE_EXTENSIONS:
        candidate = data_dir / ("cover" + ext)
        if candidate.exists():
            return candidate
    # 3. any image in the folder
    for ext in IMAGE_EXTENSIONS:
        found = list(data_dir.glob(f"*{ext}"))
        if found:
            return found[0]
    return None


def generate_config(data_dir: Path, clip_duration_default=30):
    config_path = data_dir / "config.json"
    if config_path.exists():
        answer = input(f"config.json already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)
        config_path.unlink()

    wav_files = sorted(data_dir.glob("*.wav"))
    if not wav_files:
        print(f"No WAV files found in {data_dir}", file=sys.stderr)
        sys.exit(1)

    # pick a representative image for the accent color (first track's image or any image)
    first_image = _find_image_for_track(wav_files[0], data_dir)
    auto_accent = get_dominant_color(first_image)
    accent_hex  = "#{:02x}{:02x}{:02x}".format(*auto_accent)
    print(f"  Detected accent color: {accent_hex}  (from {first_image.name if first_image else 'none'})")

    tracks = []
    for wav in wav_files:
        total = get_audio_duration(wav)
        auto_start = max(0.0, (total - clip_duration_default) / 2)
        img = _find_image_for_track(wav, data_dir)
        image_name = img.name if img else "cover.png"
        tracks.append({
            "audio":              wav.name,
            "image":              image_name,
            "headline":           "",
            "title":              parse_track_title(wav.name),
            "copy":               "",
            "start":              format_timecode(auto_start),
            "accent_color":       None,
            "font_color":         None,
            "font":               None,
            "overlay_color":      None,
            "overlay_opacity":    None,
            "typewriter_headline": None,
            "typewriter_title":    None,
            "typewriter_copy":     None,
            "progress_bar_color":  None,
        })
        print(f"  {wav.name}: {total:.1f}s  →  start={format_timecode(auto_start)}")

    schema_path = Path(__file__).parent / "config.schema.json"
    try:
        rel_schema = os.path.relpath(schema_path, data_dir)
    except ValueError:
        rel_schema = str(schema_path)

    cfg = {
        "$schema":       rel_schema,
        "ep_title":      data_dir.name.replace("-", " ").title(),
        "clip_duration": clip_duration_default,
        "accent_color":    accent_hex,
        "font_color":      None,
        "font":            None,
        "format":          "square",
        "overlay_color":   "#000000",
        "overlay_opacity": 0.7,
        "progress_bar":          True,
        "progress_bar_position": "top",
        "progress_bar_color":    None,
        "typewriter_headline":   False,
        "typewriter_title":      False,
        "typewriter_copy":       False,
        "tracks":                tracks,
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

    from tqdm import tqdm
    for fi in tqdm(range(n_frames), desc="    waveform", unit="fr", leave=True):
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


def _tw_visible(text, t, start_t):
    """Characters revealed so far for a typewriter effect."""
    if t is None or t < start_t:
        return ""
    return text[:max(0, int((t - start_t) * TW_CHARS_PER_SEC))]


def _fade_alpha(t, start_t, duration=TW_FADE_DURATION):
    """0.0→1.0 ramp for a fade-in effect."""
    if t is None or t < start_t:
        return 0.0
    return min(1.0, (t - start_t) / duration)


def render_frame(image_path, bar_heights, text_config, size,
                 accent_color=None, font_color=None,
                 overlay_color=None, overlay_opacity=None,
                 progress=None, progress_bar_top=True, font_path=None,
                 typewriter_t=None,
                 typewriter_headline=False, typewriter_title=False, typewriter_copy=False,
                 progress_bar_color=None):
    w, h = size
    ac = accent_color or (255, 255, 255)
    oc = overlay_color or (0, 0, 0)
    max_alpha = int((overlay_opacity if overlay_opacity is not None else 0.7) * 255)

    if image_path and Path(image_path).exists():
        img = _crop_and_resize(Image.open(image_path).convert("RGB"), w, h)
    else:
        img = Image.new("RGB", (w, h), (20, 20, 30))

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    gradient_top = int(h * 0.25)
    for y_pos in range(gradient_top, h):
        p = (y_pos - gradient_top) / (h - gradient_top)
        draw_ov.line([(0, y_pos), (w, y_pos)], fill=(*oc, int(max_alpha * (p ** 2.8))))

    frame = Image.alpha_composite(img.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(frame)

    if bar_heights is not None and len(bar_heights) > 0:
        bar_w = max(2, (w - 2 * PADDING_X - (N_BARS - 1) * BAR_GAP) // N_BARS)
        total_bar_w = N_BARS * bar_w + (N_BARS - 1) * BAR_GAP
        x0 = (w - total_bar_w) // 2
        cy = int(h * 0.52)
        waveform_fill = (*ac, 200)
        for i, height_norm in enumerate(bar_heights):
            bh = max(3, int(height_norm * MAX_BAR_HEIGHT))
            x = x0 + i * (bar_w + BAR_GAP)
            draw.rectangle([x, cy - bh, x + bar_w, cy + bh], fill=waveform_fill)

    if progress is not None:
        bar_h = 5
        bar_y = 20 if progress_bar_top else h - 28
        pbc = progress_bar_color or (255, 255, 255)
        draw.rectangle([0, bar_y, w, bar_y + bar_h], fill=(*pbc, 60))
        if progress > 0:
            draw.rectangle([0, bar_y, int(w * progress), bar_y + bar_h], fill=(*ac, 220))

    font_headline = get_font(22, font_path=font_path)
    font_title    = get_font(58, bold=True, font_path=font_path)
    font_copy     = get_font(26, font_path=font_path)

    headline_full = text_config.get("headline", "")
    title_full    = text_config.get("title", "")
    copy_full     = text_config.get("copy", "")

    # animation timing chain
    t = typewriter_t
    headline_start  = 0.3
    headline_done   = headline_start + (TW_FADE_DURATION if typewriter_headline and headline_full else 0)
    title_tw_start  = headline_done  + (0.2             if typewriter_title    and title_full    else 0)
    title_done      = title_tw_start + (len(title_full) / TW_CHARS_PER_SEC if typewriter_title  else 0)
    copy_start      = title_done     + (0.2             if typewriter_copy     and copy_full     else 0)

    def headline_alpha(): return _fade_alpha(t, headline_start) if typewriter_headline else 1.0
    def title_alpha():    return 1.0  # title is typewriter, always fully opaque once visible
    def copy_alpha():     return _fade_alpha(t, copy_start)     if typewriter_copy     else 1.0

    title_visible = _tw_visible(title_full, t, title_tw_start) if typewriter_title else title_full

    # build blocks — always include if full text exists so layout stays stable
    blocks = []
    if not text_config.get("no_text"):
        if headline_full:
            blocks.append(("headline", headline_full.upper(), font_headline, headline_full, headline_alpha()))
        if title_full:
            blocks.append(("title",    title_visible,          font_title,   title_full,    title_alpha()))
        if copy_full:
            blocks.append(("copy",     copy_full,              font_copy,    copy_full,     copy_alpha()))

    # anchor layout to full text so nothing shifts during animation
    total_text_h = sum(draw.textbbox((0, 0), full, font=font)[3] for _, _, font, full, _ in blocks)
    total_text_h += 12 * (len(blocks) - 1)
    text_y = h - 80 - total_text_h

    fc = font_color or (255, 255, 255)
    base_colors = {
        "headline": (*ac, 230),
        "title":    (*fc, 255),
        "copy":     (int(fc[0]*0.75), int(fc[1]*0.75), int(fc[2]*0.75), 200),
    }
    # Draw text onto a separate RGBA layer so alpha fades composite correctly.
    # draw.text() on an RGBA image does direct pixel writes — convert("RGB") then
    # drops the alpha, making text visible even at alpha=0. alpha_composite fixes this.
    text_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_text  = ImageDraw.Draw(text_layer)
    for kind, text, font, full_text, alpha in blocks:
        if text and alpha > 0:
            r, g, b, a = base_colors[kind]
            color = (r, g, b, int(a * alpha))
            bbox = draw.textbbox((0, 0), text, font=font)
            x = max(PADDING_X, (w - (bbox[2] - bbox[0])) // 2)
            draw_text.text((x, text_y), text, font=font, fill=color)
        text_y += draw.textbbox((0, 0), full_text, font=font)[3] + 12

    frame = Image.alpha_composite(frame, text_layer)
    return frame.convert("RGB")


# ── Output ────────────────────────────────────────────────────────────────────

def resolve_accent(track, ep_accent=None, cli_accent=None) -> tuple:
    """Return accent color: CLI > per-track config > EP config > auto-extracted."""
    return (
        cli_accent
        or track.get("accent_color")
        or ep_accent
        or get_dominant_color(track["image"])
    )


def resolve_font(track, ep_font=None, cli_font=None):
    """Return font path: CLI > per-track config > EP config > None (system font)."""
    return cli_font or track.get("font") or ep_font or None


def resolve_font_color(track, ep_font_color=None, cli_font_color=None):
    """Return font color: CLI > per-track config > EP config > None (default white)."""
    return cli_font_color or track.get("font_color") or ep_font_color or None


def resolve_overlay(track, ep_overlay_color=None, ep_overlay_opacity=None,
                    cli_overlay_color=None, cli_overlay_opacity=None):
    """Return (overlay_color, overlay_opacity): CLI > per-track > EP > None (defaults in render_frame)."""
    color   = cli_overlay_color   or track.get("overlay_color")   or ep_overlay_color   or None
    opacity = cli_overlay_opacity or track.get("overlay_opacity") or ep_overlay_opacity or None
    return color, opacity

def save_preview(track, export_dir: Path, fmt: str, ep_accent=None, ep_font=None,
                 ep_font_color=None, ep_overlay_color=None, ep_overlay_opacity=None,
                 cli_accent=None, cli_font=None, cli_font_color=None,
                 cli_overlay_color=None, cli_overlay_opacity=None, **_):
    size = FORMATS["square" if fmt == "carousel" else fmt]
    bars = np.full(N_BARS, 0.4)
    bars[N_BARS // 4: 3 * N_BARS // 4] = 0.8
    ov_color, ov_opacity = resolve_overlay(track, ep_overlay_color, ep_overlay_opacity,
                                           cli_overlay_color, cli_overlay_opacity)
    out_path = export_dir / f"{slugify(track['title'])}_preview.png"
    render_frame(track["image"], bars, track, size,
                 accent_color=resolve_accent(track, ep_accent, cli_accent),
                 font_color=resolve_font_color(track, ep_font_color, cli_font_color),
                 overlay_color=ov_color, overlay_opacity=ov_opacity,
                 font_path=resolve_font(track, ep_font, cli_font)).save(out_path)
    print(f"  Preview saved: {out_path}")


def save_carousel_slide(track, index, carousel_dir: Path, force=False,
                        ep_accent=None, ep_font=None, ep_font_color=None,
                        ep_overlay_color=None, ep_overlay_opacity=None,
                        cli_accent=None, cli_font=None, cli_font_color=None,
                        cli_overlay_color=None, cli_overlay_opacity=None, **_):
    out_path = carousel_dir / f"{index:02d}_{slugify(track['title'])}.png"
    if out_path.exists() and not force:
        print(f"  Skipping (exists): {out_path.name}  —  use --force to overwrite")
        return
    ov_color, ov_opacity = resolve_overlay(track, ep_overlay_color, ep_overlay_opacity,
                                           cli_overlay_color, cli_overlay_opacity)
    render_frame(track["image"], None, track, FORMATS["carousel"],
                 accent_color=resolve_accent(track, ep_accent, cli_accent),
                 font_color=resolve_font_color(track, ep_font_color, cli_font_color),
                 overlay_color=ov_color, overlay_opacity=ov_opacity,
                 font_path=resolve_font(track, ep_font, cli_font)).save(out_path)
    print(f"  Carousel slide saved: {out_path}")


def create_video(track, export_dir: Path, fmt: str, clip_duration, explicit_start,
                 force=False, show_progress_bar=False, show_waveform=True,
                 ep_accent=None, ep_font=None, ep_font_color=None,
                 ep_overlay_color=None, ep_overlay_opacity=None,
                 cli_accent=None, cli_font=None, cli_font_color=None,
                 cli_overlay_color=None, cli_overlay_opacity=None,
                 progress_bar_top=True,
                 ep_typewriter_headline=False, ep_typewriter_title=False, ep_typewriter_copy=False,
                 ep_progress_bar_color=None):
    from moviepy import VideoClip, AudioFileClip
    from moviepy.audio.fx import AudioFadeIn, AudioFadeOut  # type: ignore

    out_path = export_dir / f"{slugify(track['title'])}.mp4"
    if out_path.exists() and not force:
        print(f"  Skipping (exists): {out_path.name}  —  use --force to overwrite")
        return

    size = FORMATS[fmt]
    audio_path = track["audio"]
    start_time = resolve_start_time(
        audio_path, clip_duration,
        explicit_start if explicit_start is not None else track.get("start"),
    )
    accent              = resolve_accent(track, ep_accent, cli_accent)
    font_color          = resolve_font_color(track, ep_font_color, cli_font_color)
    font_path           = resolve_font(track, ep_font, cli_font)
    ov_color, ov_opacity = resolve_overlay(track, ep_overlay_color, ep_overlay_opacity,
                                            cli_overlay_color, cli_overlay_opacity)
    tw_headline = track.get("typewriter_headline") if track.get("typewriter_headline") is not None else ep_typewriter_headline
    tw_title    = track.get("typewriter_title")    if track.get("typewriter_title")    is not None else ep_typewriter_title
    tw_copy     = track.get("typewriter_copy")     if track.get("typewriter_copy")     is not None else ep_typewriter_copy
    pb_color        = track.get("progress_bar_color")  or ep_progress_bar_color or None

    if show_waveform:
        print(f"  Extracting waveform from {audio_path.name} (start={start_time:.1f}s)...")
        waveform_frames = extract_waveform_frames(audio_path, start_time, clip_duration, FPS, N_BARS)
    else:
        waveform_frames = None
    n_frames = int(clip_duration * FPS) if waveform_frames is None else len(waveform_frames)
    actual_duration = n_frames / FPS

    def make_frame(t):
        fi = min(int(t * FPS), n_frames - 1)
        progress = (t / actual_duration) if show_progress_bar else None
        bar_heights = waveform_frames[fi] if waveform_frames is not None else None
        return np.array(render_frame(
            track["image"], bar_heights, track, size,
            accent_color=accent, font_color=font_color,
            overlay_color=ov_color, overlay_opacity=ov_opacity,
            progress=progress, progress_bar_top=progress_bar_top, font_path=font_path,
            typewriter_t=t, typewriter_headline=tw_headline, typewriter_title=tw_title, typewriter_copy=tw_copy,
            progress_bar_color=pb_color,
        ))

    print(f"  Rendering {actual_duration:.1f}s at {FPS}fps ({n_frames} frames)...")
    from tqdm import tqdm
    pbar = tqdm(total=n_frames, desc="    rendering", unit="fr")
    _orig_make_frame = make_frame
    def make_frame(t):
        frame = _orig_make_frame(t)
        pbar.update(1)
        return frame
    video = VideoClip(make_frame, duration=actual_duration)

    audio = AudioFileClip(str(audio_path))
    audio = audio.subclipped(start_time, min(start_time + actual_duration, audio.duration))
    audio = audio.with_effects([AudioFadeIn(1.5), AudioFadeOut(1.5)])
    video = video.with_audio(audio)

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
    parser.add_argument("--format", choices=["square", "reel", "carousel", "all"], default=None,
                        help="square=1080×1080 MP4, reel=1080×1920 MP4, carousel=static PNGs, all=every format (default: square)")
    parser.add_argument("--preview", action="store_true",
                        help="Render a static PNG preview per track, skip MP4 encoding")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing output files")
    parser.add_argument("--progress-bar", action="store_true",
                        help="Show an audio progress bar (default position: top)")
    parser.add_argument("--progress-bar-position", choices=["top", "bottom"], default=None,
                        dest="progress_bar_position",
                        help="Position of the progress bar: top (default) or bottom")
    parser.add_argument("--font", type=str, default=None,
                        help="Path to a .ttf font file to use instead of the system font")
    parser.add_argument("--accent-color", type=str, default=None, dest="accent_color",
                        help="Accent color as hex '#ff6b35' or comma-separated RGB '255,107,53'. "
                             "Overrides config.json and auto-extraction.")
    parser.add_argument("--font-color", type=str, default=None, dest="font_color",
                        help="Text color for title and copy as hex '#ffffff' or '255,255,255'. "
                             "Default: white.")
    parser.add_argument("--overlay-color", type=str, default=None, dest="overlay_color",
                        help="Gradient overlay color as hex or r,g,b. Default: #000000.")
    parser.add_argument("--overlay-opacity", type=float, default=None, dest="overlay_opacity",
                        help="Gradient overlay strength 0.0–1.0. Default: 0.7.")
    parser.add_argument("--no-waveform", action="store_true", dest="no_waveform",
                        help="Disable the animated waveform visualisation.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"Error: directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    if args.init:
        print(f"\nGenerating config.json for {data_dir.name}...\n")
        generate_config(data_dir, clip_duration_default=int(args.duration or 30))
        sys.exit(0)

    ep_name, json_duration, tracks, extras = load_config(data_dir)
    clip_duration   = args.duration or json_duration
    output_format   = args.format or extras.get("format") or "square"
    if output_format not in {*FORMATS, "all"}:
        print(f"Error: invalid format '{output_format}' in config.json (use square, reel, carousel, all)", file=sys.stderr)
        sys.exit(1)
    active_formats = list(FORMATS.keys()) if output_format == "all" else [output_format]
    explicit_start  = parse_timecode(args.start)
    show_progress    = args.progress_bar or extras.get("progress_bar", False)
    show_waveform    = False if args.no_waveform else extras.get("waveform", True)
    pb_position      = args.progress_bar_position or extras.get("progress_bar_position", "top")
    progress_bar_top = pb_position != "bottom"
    ep_font          = extras.get("font") or None
    ep_font_color    = extras.get("font_color") or None
    ep_accent        = extras.get("accent_color") or None
    cli_font         = args.font or None

    def _parse_cli_color(raw):
        if not raw:
            return None
        return parse_color([int(x) for x in raw.split(",")] if "," in raw else raw)

    cli_accent          = _parse_cli_color(args.accent_color)
    cli_font_color      = _parse_cli_color(args.font_color)
    cli_overlay_color   = _parse_cli_color(args.overlay_color)
    cli_overlay_opacity = args.overlay_opacity
    ep_overlay_color      = extras.get("overlay_color") or None
    ep_overlay_opacity    = extras.get("overlay_opacity")
    ep_typewriter_headline = extras.get("typewriter_headline", False)
    ep_typewriter_title    = extras.get("typewriter_title", False)
    ep_typewriter_copy     = extras.get("typewriter_copy", False)
    ep_progress_bar_color  = extras.get("progress_bar_color") or None

    project_root = Path(__file__).parent

    mode = "preview" if args.preview else output_format
    print(f"\n  release     : {ep_name}")
    print(f"  format      : {output_format}  |  mode: {mode}")
    if clip_duration:
        print(f"  duration    : {clip_duration}s")
    if cli_font or ep_font:
        print(f"  font        : {cli_font or ep_font}")
    if cli_accent or ep_accent:
        ac = cli_accent or ep_accent
        print(f"  accent      : #{ac[0]:02x}{ac[1]:02x}{ac[2]:02x}")
    if cli_font_color or ep_font_color:
        fc = cli_font_color or ep_font_color
        print(f"  font color  : #{fc[0]:02x}{fc[1]:02x}{fc[2]:02x}")
    print(f"  waveform    : {'on' if show_waveform else 'off'}")
    print(f"  progress bar: {'on' if show_progress else 'off'}")
    print(f"  force       : {'on' if args.force else 'off'}")
    print(f"  tracks      : {len(tracks)}")
    print()

    kwargs = dict(show_waveform=show_waveform,
                  ep_accent=ep_accent, ep_font=ep_font, ep_font_color=ep_font_color,
                  ep_overlay_color=ep_overlay_color, ep_overlay_opacity=ep_overlay_opacity,
                  cli_accent=cli_accent, cli_font=cli_font, cli_font_color=cli_font_color,
                  cli_overlay_color=cli_overlay_color, cli_overlay_opacity=cli_overlay_opacity,
                  progress_bar_top=progress_bar_top,
                  ep_typewriter_headline=ep_typewriter_headline,
                  ep_typewriter_title=ep_typewriter_title,
                  ep_typewriter_copy=ep_typewriter_copy,
                  ep_progress_bar_color=ep_progress_bar_color)

    for fmt in active_formats:
        export_dir = (project_root / "export" / ep_name / fmt).resolve()
        export_dir.mkdir(parents=True, exist_ok=True)
        if len(active_formats) > 1:
            print(f"── {fmt} ──────────────────────────────")

        for i, track in enumerate(tracks, 1):
            print(f"[{i}/{len(tracks)}] {track['title']}")
            if not track["audio"] or not Path(track["audio"]).exists():
                print("  WARNING: audio file not found, skipping.")
                continue
            if args.preview:
                save_preview(track, export_dir, fmt, **kwargs)
            elif fmt == "carousel":
                save_carousel_slide(track, i, export_dir, force=args.force, **kwargs)
            else:
                create_video(track, export_dir, fmt, clip_duration, explicit_start,
                             force=args.force, show_progress_bar=show_progress, **kwargs)

    print("\nDone.")


if __name__ == "__main__":
    main()

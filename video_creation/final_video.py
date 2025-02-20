import multiprocessing
import os
import re
import tempfile
import textwrap
import threading
import time
from os.path import exists  # Needs to be imported specifically
from pathlib import Path
from typing import Dict, Final, Tuple

import ffmpeg
import translators
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.progress import track

from utils import settings
from utils.cleanup import cleanup
from utils.console import print_step, print_substep
from utils.fonts import getheight
from utils.id import extract_id
from utils.thumbnail import create_thumbnail
from utils.videos import save_data

console = Console()


class ProgressFfmpeg(threading.Thread):
    def __init__(self, vid_duration_seconds, progress_update_callback):
        threading.Thread.__init__(self, name="ProgressFfmpeg")
        self.stop_event = threading.Event()
        self.output_file = tempfile.NamedTemporaryFile(mode="w+", delete=False)
        self.vid_duration_seconds = vid_duration_seconds
        self.progress_update_callback = progress_update_callback

    def run(self):
        while not self.stop_event.is_set():
            latest_progress = self.get_latest_ms_progress()
            if latest_progress is not None:
                completed_percent = latest_progress / self.vid_duration_seconds
                self.progress_update_callback(completed_percent)
            time.sleep(1)

    def get_latest_ms_progress(self):
        lines = self.output_file.readlines()

        if lines:
            for line in lines:
                if "out_time_ms" in line:
                    out_time_ms_str = line.split("=")[1].strip()
                    if out_time_ms_str.isnumeric():
                        return float(out_time_ms_str) / 1000000.0
                    else:
                        # Handle the case when "N/A" is encountered
                        return None
        return None

    def stop(self):
        self.stop_event.set()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args, **kwargs):
        self.stop()


def name_normalize(name: str) -> str:
    name = re.sub(r'[?\\"%*:|<>]', "", name)
    name = re.sub(r"( [w,W]\s?\/\s?[o,O,0])", r" without", name)
    name = re.sub(r"( [w,W]\s?\/)", r" with", name)
    name = re.sub(r"(\d+)\s?\/\s?(\d+)", r"\1 of \2", name)
    name = re.sub(r"(\w+)\s?\/\s?(\w+)", r"\1 or \2", name)
    name = re.sub(r"\/", r"", name)

    lang = settings.config["reddit"]["thread"]["post_lang"]
    if lang:
        print_substep("Translating filename...")
        translated_name = translators.translate_text(name, translator="google", to_language=lang)
        return translated_name
    else:
        return name


def prepare_background(reddit_id: str, W: int, H: int) -> str:
    """Prepares the background video by cropping and encoding it appropriately.
    
    Args:
        reddit_id (str): The Reddit post ID
        W (int): Target width
        H (int): Target height
        
    Returns:
        str: Path to the prepared background video file
    """
    import ffmpeg
    import platform
    import multiprocessing
    
    output_path = f"assets/temp/{reddit_id}/background_noaudio.mp4"
    
    # Base video processing configuration
    input_stream = ffmpeg.input(f"assets/temp/{reddit_id}/background.mp4")
    video = input_stream.filter("crop", f"ih*({W}/{H})", "ih")
    
    # Determine system-appropriate encoder settings
    system = platform.system().lower()
    
    if system == "darwin":  # macOS
        # Use VideoToolbox hardware encoding if available
        output_settings = {
            "c:v": "h264_videotoolbox",  # macOS hardware encoder
            "b:v": "20M",
            "b:a": "192k",
            "threads": multiprocessing.cpu_count(),
        }
    elif system == "windows":
        # Try NVIDIA encoder first, fall back to CPU encoding if not available
        try:
            output = (
                video.output(
                    output_path,
                    **{
                        "c:v": "h264_nvenc",
                        "b:v": "20M",
                        "b:a": "192k",
                        "threads": multiprocessing.cpu_count(),
                    }
                )
                .overwrite_output()
            )
            output.run(quiet=True)
            return output_path
        except ffmpeg.Error:
            output_settings = {
                "c:v": "libx264",  # CPU-based encoder
                "b:v": "20M",
                "b:a": "192k",
                "threads": multiprocessing.cpu_count(),
            }
    else:  # Linux or other systems
        # Use CPU encoding as a safe default
        output_settings = {
            "c:v": "libx264",
            "b:v": "20M",
            "b:a": "192k",
            "threads": multiprocessing.cpu_count(),
        }
    
    try:
        output = (
            video.output(output_path, an=None, **output_settings)
            .overwrite_output()
        )
        output.run(quiet=True)
    except ffmpeg.Error as e:
        print(f"FFmpeg encoding error: {e.stderr.decode('utf8') if e.stderr else str(e)}")
        raise
        
    return output_path

def get_text_height(draw, text, font, max_width):
    lines = textwrap.wrap(text, width=max_width)
    total_height = 0
    for line in lines:
        _, _, _, height = draw.textbbox((0, 0), line, font=font)
        total_height += height
    return total_height


def create_fancy_thumbnail(image, text, text_color, padding, wrap=35):
    """
    Create a dynamic thumbnail with resizable middle section and text overlay.
    """
    print_step(f"Creating fancy thumbnail for: {text}")
    font_title_size = 47
    font = ImageFont.truetype(os.path.join("fonts", "Roboto-Bold.ttf"), font_title_size)
    image_width, image_height = image.size

    # Calculate text height to determine new image height
    draw = ImageDraw.Draw(image)
    text_height = get_text_height(draw, text, font, wrap)
    lines = textwrap.wrap(text, width=wrap)
    new_image_height = image_height + text_height + padding * (len(lines) - 1) - 50

    # Separate the image into top, middle (1px), and bottom parts
    top_part_height = image_height // 2
    middle_part_height = 1  # 1px height middle section
    bottom_part_height = image_height - top_part_height - middle_part_height

    top_part = image.crop((0, 0, image_width, top_part_height))
    middle_part = image.crop((0, top_part_height, image_width, top_part_height + middle_part_height))
    bottom_part = image.crop((0, top_part_height + middle_part_height, image_width, image_height))

    # Stretch the middle part
    new_middle_height = new_image_height - top_part_height - bottom_part_height
    middle_part = middle_part.resize((image_width, new_middle_height))

    # Create new image with the calculated height
    new_image = Image.new("RGBA", (image_width, new_image_height))

    # Paste the top, stretched middle, and bottom parts into the new image
    new_image.paste(top_part, (0, 0))
    new_image.paste(middle_part, (0, top_part_height))
    new_image.paste(bottom_part, (0, top_part_height + new_middle_height))

    # Draw the title text on the new image
    draw = ImageDraw.Draw(new_image)
    y = top_part_height + padding * 2  # Increased initial padding
    for line in lines:
        draw.text((120, y), line, font=font, fill=text_color, align="left")
        y += get_text_height(draw, line, font, wrap) + padding

    # Draw the username with verification icon
    username_font = ImageFont.truetype(os.path.join("fonts", "Roboto-Bold.ttf"), 30)
    channel_name = settings.config["settings"]["channel_name"]
    
    # Load verification icon
    verify_icon = Image.open(os.path.join("assets", "verify_icon.png"))
    verify_icon = verify_icon.resize((30, 30))  # Adjust size as needed

    # Calculate positioning
    username_width = draw.textlength(channel_name, font=username_font)
    total_width = username_width + verify_icon.width + 10  # 10px spacing
    start_x = 205  # Kept original x-position

    # Draw username
    draw.text(
        (start_x, 825),
        channel_name,
        font=username_font,
        fill=text_color,
        align="left"
    )

    # Paste verification icon
    new_image.paste(verify_icon, (start_x + int(username_width) + 10, 825), verify_icon)

    return new_image

def merge_background_audio(audio: ffmpeg, reddit_id: str):
    """Gather an audio and merge with assets/backgrounds/background.mp3
    Args:
        audio (ffmpeg): The TTS final audio but without background.
        reddit_id (str): The ID of subreddit
    """
    background_audio_volume = settings.config["settings"]["background"]["background_audio_volume"]
    if background_audio_volume == 0:
        return audio  # Return the original audio
    else:
        # sets volume to config
        bg_audio = ffmpeg.input(f"assets/temp/{reddit_id}/background.mp3").filter(
            "volume",
            background_audio_volume,
        )
        # Merges audio and background_audio
        merged_audio = ffmpeg.filter([audio, bg_audio], "amix", duration="longest")
        return merged_audio  # Return merged audio


def make_final_video(
    number_of_clips: int,
    length: int,
    reddit_obj: dict,
    background_config: Dict[str, Tuple],
):
    """Creates final video with templated title and timed overlays"""
    W: Final[int] = int(settings.config["settings"]["resolution_w"])
    H: Final[int] = int(settings.config["settings"]["resolution_h"])
    opacity = settings.config["settings"]["opacity"]
    reddit_id = extract_id(reddit_obj)

    allowOnlyTTSFolder: bool = (
        settings.config["settings"]["background"]["enable_extra_audio"]
        and settings.config["settings"]["background"]["background_audio_volume"] != 0
    )

    print_step("Creating the final video 🎥")

    background_clip = ffmpeg.input(prepare_background(reddit_id, W=W, H=H))

    # Create title using template
    title_template = Image.open("assets/title_template.png")
    title = reddit_obj["thread_title"]
    title = name_normalize(title)
    font_color = "#000000"
    padding = 5

    title_img = create_fancy_thumbnail(title_template, title, font_color, padding)
    title_img.save(f"assets/temp/{reddit_id}/png/title.png")

    # Get title audio duration to use for timing
    title_duration = float(
        ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"]["duration"]
    )

    # Gather audio clips
    if number_of_clips == 0 and settings.config["settings"]["storymode"] == "false":
        print("No audio clips to gather. Please use a different TTS or post.")
        exit()

    # Add ding sound effect
    ding_audio = ffmpeg.input("assets/ding.mp3")
    
    # Prepare title audio
    title_audio = ffmpeg.input(f"assets/temp/{reddit_id}/mp3/title.mp3")

    # Prepare other audio clips
    audio_clips = []
    if settings.config["settings"]["storymode"]:
        if settings.config["settings"]["storymodemethod"] == 0:
            audio_clips.append(ffmpeg.input(f"assets/temp/{reddit_id}/mp3/postaudio.mp3"))
        elif settings.config["settings"]["storymodemethod"] == 1:
            audio_clips.extend([
                ffmpeg.input(f"assets/temp/{reddit_id}/mp3/postaudio-{i}.mp3")
                for i in track(range(number_of_clips + 1), "Collecting the audio files...")
            ])
    else:
        audio_clips.extend([
            ffmpeg.input(f"assets/temp/{reddit_id}/mp3/{i}.mp3") 
            for i in range(number_of_clips)
        ])

    # Get title duration
    title_duration = float(
        ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"]["duration"]
    )

    # Mix ding and title audio together
    mixed_title = ffmpeg.filter(
        [ding_audio, title_audio], 
        'amix', 
        inputs=2,
        duration='longest'  # Use longest to ensure title audio finishes
    )
    
    # # Add silence padding to ensure mixed_title reaches full title_duration
    # mixed_title = ffmpeg.filter(
    #     [mixed_title],
    #     'apad',
    #     pad_dur=title_duration
    # )

    # Concatenate with remaining audio clips - they'll start after title finishes
    all_audio = [mixed_title] + audio_clips
    audio_concat = ffmpeg.concat(*all_audio, a=1, v=0)
    final_audio = merge_background_audio(audio_concat, reddit_id)

    console.log(f"[bold green] Video Will Be: {length} Seconds Long")

    # Handle title overlay with animations
    screenshot_width = int((W * 45) // 100)
    title_img = ffmpeg.input(f"assets/temp/{reddit_id}/png/title.png")["v"]
    title_img = title_img.filter("scale", screenshot_width, -1)
    title_img = title_img.filter("colorchannelmixer", aa=opacity)

    # Animation timings
    start_time = 0
    zoom_in_duration = 0.75  # Time to zoom in
    slide_out_duration = 0.75  # Time to slide out
    end_time = title_duration

    # Add scale effect through transform filter 
    animated_title = title_img.filter(
        'scale',  # Basic size
        screenshot_width,
        -1
    )

    # Position and slide calculations
    y_offset = -100  # Move up by 100 pixels
    pos_x = f'if(gt(t,{end_time - slide_out_duration}), ' + \
           f'(W-w)/2-(2*W)*(t-{end_time - slide_out_duration})/{slide_out_duration}, ' + \
           f'(W-w)/2)'
    pos_y = f'(H-h)/2-100'

    transform_expr = f'transform=\'scale=if(lt(t,{zoom_in_duration}),0.5+t/{zoom_in_duration}/2,1)\''

    # Apply transforms and overlay
    background_clip = background_clip.overlay(
        animated_title,
        enable=f'between(t,{start_time},{end_time})',
        x=pos_x,
        y=pos_y,
        eval='frame',
        format='yuv420'
    )

    # Add background credit
    background_clip = ffmpeg.drawtext(
        background_clip,
        text=f"Background by {background_config['video'][2]}",
        x=f"(w-text_w)",
        y=f"(h-text_h)",
        fontsize=5,
        fontcolor="White",
        fontfile=os.path.join("fonts", "Roboto-Regular.ttf"),
    )
    
    background_clip = background_clip.filter("scale", W, H)

    print_step("Rendering the video 🎥")
    from tqdm import tqdm

    pbar = tqdm(total=100, desc="Progress: ", bar_format="{l_bar}{bar}", unit=" %")

    def on_update_example(progress) -> None:
        status = round(progress * 100, 2)
        old_percentage = pbar.n
        pbar.update(status - old_percentage)

    title = extract_id(reddit_obj, "thread_title")
    idx = extract_id(reddit_obj)
    title_thumb = reddit_obj["thread_title"]
    filename = f"{name_normalize(title)[:251]}"
    subreddit = settings.config["reddit"]["thread"]["subreddit"]

    # Create required directories
    if not exists(f"./results/{subreddit}"):
        print_substep("The 'results' folder could not be found so it was automatically created.")
        os.makedirs(f"./results/{subreddit}")

    if not exists(f"./results/{subreddit}/OnlyTTS") and allowOnlyTTSFolder:
        print_substep("The 'OnlyTTS' folder could not be found so it was automatically created.")
        os.makedirs(f"./results/{subreddit}/OnlyTTS")

    # Render main video
    defaultPath = f"results/{subreddit}"
    with ProgressFfmpeg(length, on_update_example) as progress:
        path = defaultPath + f"/{filename}"
        path = path[:251] + ".mp4"
        try:
            ffmpeg.output(
                background_clip,
                final_audio,
                path,
                f="mp4",
                **{
                    "c:v": "h264_videotoolbox",
                    "b:v": "20M",
                    "b:a": "192k",
                    "threads": multiprocessing.cpu_count(),
                },
            ).overwrite_output().global_args("-progress", progress.output_file.name).run(
                quiet=True,
                overwrite_output=True,
                capture_stdout=False,
                capture_stderr=False,
            )
        except ffmpeg.Error as e:
            print(e.stderr.decode("utf8"))
            exit(1)

    old_percentage = pbar.n
    pbar.update(100 - old_percentage)

    # Handle OnlyTTS version if enabled
    if allowOnlyTTSFolder:
        path = defaultPath + f"/OnlyTTS/{filename}"
        path = path[:251] + ".mp4"
        print_step("Rendering the Only TTS Video 🎥")
        with ProgressFfmpeg(length, on_update_example) as progress:
            try:
                ffmpeg.output(
                    background_clip,
                    audio_concat,  # Use concat audio without background music
                    path,
                    f="mp4",
                    **{
                        "c:v": "h264_videotoolbox",
                        "b:v": "20M",
                        "b:a": "192k",
                        "threads": multiprocessing.cpu_count(),
                    },
                ).overwrite_output().global_args("-progress", progress.output_file.name).run(
                    quiet=True,
                    overwrite_output=True,
                    capture_stdout=False,
                    capture_stderr=False,
                )
            except ffmpeg.Error as e:
                print(e.stderr.decode("utf8"))
                exit(1)

        old_percentage = pbar.n
        pbar.update(100 - old_percentage)

    pbar.close()

    save_data(subreddit, filename + ".mp4", title, idx, background_config["video"][2])
    print_step("Removing temporary files 🗑")
    cleanups = cleanup(reddit_id)
    print_substep(f"Removed {cleanups} temporary files 🗑")
    print_step("Done! 🎉 The video is in the results folder 📁")


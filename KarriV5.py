import time
import board
import busio
import subprocess
import digitalio
import math

from subprocess import DEVNULL

# display
from PIL import Image, ImageDraw, ImageFont
from adafruit_ht16k33.ht16k33 import HT16K33

# ADC
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# ——— Shared I2C bus & peripherals —————————————————————————————
i2c = busio.I2C(board.SCL, board.SDA)

# LED matrices
addresses = [0x70, 0x71, 0x72, 0x73]
matrices  = [HT16K33(i2c, address=a) for a in addresses]
# LED Brightness 0=lowest 1=Highest
for m in matrices:
    m.brightness = 1

# ADC for joystick Y
ads           = ADS.ADS1115(i2c, address=0x48)
ads.gain      = 1
ads.data_rate = 8
y_chan        = AnalogIn(ads, ADS.P1)

# ——— Audio & joystick thresholds —————————————————————————————
AUDIO_FILE      = "/tmp/joystick_audio.wav"
MIC_DEVICE      = "default"
# Joy‑Con thresholds
RECORD_THRESHOLD = 3.0   # anything above ~3 V → record
CANCEL_THRESHOLD = 1.2   # anything below ~1 V → cancel


prev_y_state    = 'neutral'
arecord_proc    = None

# ——— Volume settings —————————————————————————————————————————————
VOLUME_MIN   = 0
VOLUME_MAX   = 100
VOLUME_STEP  = 20

# start at 80%
volume_level = 80
subprocess.run(
    ["amixer", "sset", "Master", f"{volume_level}%"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)


# ——— Display diff buffer —————————————————————————————————————————
prev_pixels = [[0]*11 for _ in range(30)]

# ——— Volume state ———————————————————————————————————————————————
showing_volume          = False
last_volume_button_time = 0

# Inactivity timeout tracking
last_activity_time = time.time()
INACTIVITY_TIMEOUT = 120
# ——— Ripple & send/cancel state flags —————————————————————————
in_ripple_mode = False
awaiting_send  = False
showing_cancel = False

# ——— Buttons —————————————————————————————————————————————————————
button_up      = digitalio.DigitalInOut(board.D17); button_up.direction = digitalio.Direction.INPUT;  button_up.pull = digitalio.Pull.UP
button_down    = digitalio.DigitalInOut(board.D27); button_down.direction = digitalio.Direction.INPUT;  button_down.pull = digitalio.Pull.UP
volume_up      = digitalio.DigitalInOut(board.D23); volume_up.direction = digitalio.Direction.INPUT;     volume_up.pull = digitalio.Pull.UP
volume_down    = digitalio.DigitalInOut(board.D24); volume_down.direction = digitalio.Direction.INPUT; volume_down.pull = digitalio.Pull.UP
confirm_button = digitalio.DigitalInOut(board.D26); confirm_button.direction = digitalio.Direction.INPUT;confirm_button.pull = digitalio.Pull.UP

# ——— Helpers ——————————————————————————————————————————————————————

def clear_all():
    for m in matrices:
        m.fill(0)
        m.show()

def show_frame(image):
    """Brute‑force draw a full PIL image to the matrix."""
    for x in range(30):
        for y in range(11):
            pix = image.getpixel((x, y))
            chip, xl = x//8, x%8
            matrices[chip]._pixel(y, xl, pix)
    for m in matrices:
        m.show()

def sync_prev_pixels():
    """Resync prev_pixels to whatever’s currently on the matrices."""
    for x in range(30):
        for y in range(11):
            chip, xl = x//8, x%8
            prev_pixels[x][y] = matrices[chip]._pixel(y, xl)

def display_state_text(label):
    """
    Fast diff‑update for any text (Recording/Send?/Sent!/Cancel),
    exactly like your channel labels.
    """
    img  = Image.new("1", (30, 11))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("/home/karri/code/Fonts/nothing-font-5x7.otf", size=9)

    w    = int(draw.textlength(label, font=font))
    xoff = max(0, (30 - w)//2)
    yoff = 0
    draw.text((xoff, yoff), label, font=font, fill=255)

    dirty = set()
    for x in range(30):
        for y in range(11):
            pix = img.getpixel((x, y))
            if prev_pixels[x][y] != pix:
                chip, xl = x//8, x%8
                matrices[chip]._pixel(y, xl, pix)
                prev_pixels[x][y] = pix
                dirty.add(chip)

    for c in dirty:
        matrices[c].show()


def display_static_text(label):
    """Draw `label` centered *instantly* on the 30×11 matrix."""
    image = Image.new("1", (30, 11))
    draw  = ImageDraw.Draw(image)
    font  = ImageFont.truetype("/home/karri/code/Fonts/nothing-font-5x7.otf", size=9)

    # center horizontally
    text_width = int(draw.textlength(label, font=font))
    x = max(0, (30 - text_width) // 2)
    y = 0  # moved up 2 pixels (was 2)

    draw.text((x, y), label, font=font, fill=255)
    show_frame(image)

def display_text(label):
    """Draw `label` statically (no number) centered on the 30×11 display."""
    image = Image.new("1", (30, 11))
    draw  = ImageDraw.Draw(image)
    font  = ImageFont.truetype("/home/karri/code/Fonts/nothing-font-5x7.otf", size=9)

    # center text horizontally
    text_width  = int(draw.textlength(label, font=font))
    x_offset    = max(0, (30 - text_width) // 2)
    y_offset    = 2  # a little vertical padding

    draw.text((x_offset, y_offset), label, font=font, fill=255)
    show_frame(image)

# --- Push image to matrix ---
def show_frame(image):
    for x in range(30):
        for y in range(11):
            value = image.getpixel((x, y))
            chip = x // 8
            x_local = x % 8
            matrices[chip]._pixel(y, x_local, value)
    for m in matrices:
        m.show()

numbers_5x3 = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
}

idle_animation_frames = [
    [  # Frame 1
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111011111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
    ],
    [  # Frame 2
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111110001111111111111",
        "111111111111101110111111111111",
        "111111111111110001111111111111",
        "111111111111111011111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
    ],
    [  # Frame 3
        "111111111111111111111111111111",
        "111111111111111111111111111111",
        "111111111111100000111111111111",
        "111111111111011111011111111111",
        "111111111111011111011111111111",
        "111111111111011111011111111111",
        "111111111111100000111111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
        "111111111111111111111111111111",
        "111111111111111111111111111111",
    ],
    [  # Frame 4
        "111111111111111111111111111111",
        "111111111111000000011111111111",
        "111111111110111011101111111111",
        "111111111110110011101111111111",
        "111111111110111011101111111111",
        "111111111110111011101111111111",
        "111111111110110001101111111111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
        "111111111111111111111111111111",
    ],
    [  # Frame 5
        "111111111111000000011111111111",
        "111111111110111111101111111111",
        "111111111101111011110111111111",
        "111111111101110011110111111111",
        "111111111101111011110111111111",
        "111111111101111011110111111111",
        "111111111101110001110111111111",
        "111111111110111111101111111111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 6
        "111111111111000000011111111111",
        "111111111100111111100111111111",
        "111111111001111011110011111111",
        "111111111001110011110011111111",
        "111111111001111011110011111111",
        "111111111001111011110011111111",
        "111111111001110001110011111111",
        "111111111100111111100111111111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 7
        "111111111111000000011111111111",
        "111111111010111111101011111111",
        "111111110101111011110101111111",
        "111111110101110011110101111111",
        "111111110101111011110101111111",
        "111111110101111011110101111111",
        "111111110101110001110101111111",
        "111111111010111111101011111111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 8
        "111111111111000000011111111111",
        "111111110110111111101101111111",
        "111111101101111011110110111111",
        "111111101101110011110110111111",
        "111111101101111011110110111111",
        "111111101101111011110110111111",
        "111111101101110001110110111111",
        "111111110110111111101101111111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 9
        "111111111111000000011111111111",
        "111111101010111111101010111111",
        "111111010101111011110101011111",
        "111111010101110011110101011111",
        "111111010101111011110101011111",
        "111111010101111011110101011111",
        "111111010101110001110101011111",
        "111111101010111111101010111111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 10
        "111111111111000000011111111111",
        "111111010110111111101101011111",
        "111110101101111011110110101111",
        "111110101101110011110110101111",
        "111110101101111011110110101111",
        "111110101101111011110110101111",
        "111110101101110001110110101111",
        "111111010110111111101101011111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 11
        "111111111111000000011111111111",
        "111110101010111111101010101111",
        "111101010101111011110101010111",
        "111101010101110011110101010111",
        "111101010101111011110101010111",
        "111101010101111011110101010111",
        "111101010101110001110101010111",
        "111110101010111111101010101111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 12
        "111111111111000000011111111111",
        "111101010110111111101101010111",
        "111010101101111011110110101011",
        "111010101101110011110110101011",
        "111010101101111011110110101011",
        "111010101101111011110110101011",
        "111010101101110001110110101011",
        "111101010110111111101101010111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 13
        "111111111111000000011111111111",
        "111010101110111111101110101011",
        "110101011101111011110111010101",
        "110101011101110011110111010101",
        "110101011101111011110111010101",
        "110101011101111011110111010101",
        "110101011101110001110111010101",
        "111010101110111111101110101011",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 14
        "111111111111000000011111111111",
        "110101011110111111101111010101",
        "101010111101111011110111101010",
        "101010111101110011110111101010",
        "101010111101111011110111101010",
        "101010111101111011110111101010",
        "101010111101110001110111101010",
        "110101011110111111101111010101",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 15
        "111111111111000000011111111111",
        "101010111110111111101111101010",
        "010101111101111011110111110101",
        "010101111101110011110111110101",
        "010101111101111011110111110101",
        "010101111101111011110111110101",
        "010101111101110001110111110101",
        "101010111110111111101111110101",
        "111111111111000000011111101010",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 16
        "111111111111000000011111111111",
        "010101111110111111101111110101",
        "101011111101111011110111111010",
        "101011111101110011110111111010",
        "101011111101111011110111111010",
        "101011111101111011110111111010",
        "101011111101110001110111111010",
        "010101111110111111101111111010",
        "111111111111000000011111110101",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 17
        "111111111111000000011111111111",
        "101011111110111111101111111010",
        "010111111101111011110111111101",
        "010111111101110011110111111101",
        "010111111101111011110111111101",
        "010111111101111011110111111101",
        "010111111101110001110111111101",
        "101011111110111111101111111101",
        "111111111111000000011111111010",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 18
        "111111111111000000011111111111",
        "010111111110111111101111111101",
        "101111111101111011110111111110",
        "101111111101110011110111111110",
        "101111111101111011110111111110",
        "101111111101111011110111111110",
        "101111111101110001110111111110",
        "010111111110111111101111111110",
        "111111111111000000011111111101",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 19
        "111111111111000000011111111111",
        "101111111110111111101111111110",
        "011111111101111011110111111111",
        "011111111101110011110111111111",
        "011111111101111011110111111111",
        "011111111101111011110111111111",
        "011111111101110001110111111111",
        "101111111110111111101111111111",
        "111111111111000000011111111110",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ],
    [  # Frame 20
        "111111111111000000011111111111",
        "111111111110111111101111111111",
        "111111111101111011110111111111",
        "111111111101110011110111111111",
        "111111111101111011110111111111",
        "111111111101111011110111111111",
        "111111111101110001110111111111",
        "111111111110111111101111111111",
        "111111111111000000011111111111",
        "111111111111111000111111111111",
        "111111111111111101111111111111",
    ]

]

def play_idle_animation(frames, duration=5, frame_delay=0.25):
    global prev_pixels

    subprocess.Popen(["mpg123", "/home/karri/code/Audio Tracks/ding.mp3"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    start_time = time.time()
    while time.time() - start_time < duration:
        for frame in frames:
            dirty = set()
            for x in range(30):
                for y in range(11):
                    value = 0 if int(frame[y][x]) else 1
                    if prev_pixels[x][y] != value:
                        chip = x // 8
                        x_local = x % 8
                        matrices[chip]._pixel(y, x_local, value)
                        prev_pixels[x][y] = value
                        dirty.add(chip)
            for chip in dirty:
                matrices[chip].show()
            time.sleep(frame_delay)

    # ✅ Add new track to Mum
    new_track = "/home/karri/code/Audio Tracks/New_Message.mp3"
    if new_track not in audio_paths["Mum"]:
        audio_paths["Mum"].append(new_track)
        channel_counts["Mum"] += 1

def draw_number(image, number):
    number = str(number)
    x_offset = 29  # right-aligned
    y_offset = 0

    # Clear a 7x7 box (with 1px padding) around the number
    for x in range(26, 30):  # 6 cols
        for y in range(0, 7):  # 7 rows (5 for font + 2px top/bottom buffer)
            image.putpixel((x, y), 0)

    # Draw the number in the middle of the box
    for row_index, row in enumerate(numbers_5x3.get(number, ["000"] * 5)):
        for col_index, pixel in enumerate(row):
            if pixel == '1':
                x = x_offset - (2 - col_index)  # right to left
                y = y_offset + row_index + 1    # vertical centre (adds top padding)
                if 0 <= x < 30 and 0 <= y < 11:
                    image.putpixel((x, y), 255)
                    
def sync_prev_pixels():
    global prev_pixels
    for x in range(30):
        for y in range(11):
            chip = x // 8
            x_local = x % 8
            prev_pixels[x][y] = matrices[chip]._pixel(y, x_local)


def circular_ripple(total_duration=0.5, reverse=False):
    center_x, center_y = 15, 5
    distances = {}

    for x in range(30):
        for y in range(11):
            d = round(math.hypot(x - center_x, y - center_y), 1)
            distances.setdefault(d, []).append((x, y))

    rings = sorted(distances.keys(), reverse=reverse)
    delay = total_duration / len(rings)

    for r in rings:
        chips_to_update = set()
        for (x, y) in distances[r]:
            chip = x // 8
            x_local = x % 8
            matrices[chip]._pixel(y, x_local, 1 if not reverse else 0)
            chips_to_update.add(chip)
        for chip in chips_to_update:
            matrices[chip].show()
        time.sleep(delay)



# --- Display channel + count ---
def display_channel_label(label, count=0, speed=0.01, step=2, should_interrupt=None):
    """
    Draw `label` + counter on the 30×11 matrix, scrolling if needed.
    Returns True if the animation was aborted via should_interrupt(),
    False if it completed normally.
    """
    global prev_pixels

    # 1) Prepare font and text metrics
    font       = ImageFont.truetype("/home/karri/code/Fonts/nothing-font-5x7.otf", size=9)
    text_width = int(ImageDraw.Draw(Image.new("1", (1, 1))).textlength(label, font=font))

    def render_frame_with_number(base_img):
        img = base_img.copy()
        draw_number(img, count)
        return img

    def update_frame(img):
        dirty = set()
        for x in range(30):
            for y in range(11):
                pix = img.getpixel((x, y))
                if prev_pixels[x][y] != pix:
                    chip, xl = x // 8, x % 8
                    matrices[chip]._pixel(y, xl, pix)
                    prev_pixels[x][y] = pix
                    dirty.add(chip)
        for c in dirty:
            matrices[c].show()

    # 2) Static draw at left edge
    base = Image.new("1", (30, 11))
    ImageDraw.Draw(base).text((0, 0), label, font=font, fill=255)
    update_frame(render_frame_with_number(base))

    # 3) If it all fits, no scroll needed
    if text_width <= 25:
        return False

    # 4) Build wide image for scrolling
    scroll_image = Image.new("1", (text_width + 30, 11))
    ImageDraw.Draw(scroll_image).text((0, 0), label, font=font, fill=255)
    max_offset = text_width - 25
    offsets    = list(range(0, max_offset + 1, step))

    aborted = False

    # 5) Scroll routine (forward or backward)
    def _scroll(seq):
        nonlocal aborted
        for off in seq:
            if should_interrupt and should_interrupt():
                aborted = True
                return
            frame = scroll_image.crop((off, 0, off + 30, 11))
            update_frame(render_frame_with_number(frame))
            time.sleep(speed)

    # 6) Interruptible 1 s pause before forward scroll
    if should_interrupt:
        start = time.time()
        while time.time() - start < 1:
            if should_interrupt():
                return True
            time.sleep(0.01)
    else:
        time.sleep(1)

    # 7) Forward scroll
    _scroll(offsets)
    if aborted:
        return True

    # 8) Interruptible 1 s pause before reverse
    if should_interrupt:
        start = time.time()
        while time.time() - start < 1:
            if should_interrupt():
                return True
            time.sleep(0.01)
    else:
        time.sleep(1)

    # 9) Reverse scroll
    _scroll(reversed(offsets))
    return aborted




# --- Scroll just enough to reveal "KARRI" ---
def scroll_to_karri(message, speed=0.01, step=2):
    font = ImageFont.truetype("/home/karri/code/Fonts/nothing-font-5x7.otf", size=9)
    text_width = int(ImageDraw.Draw(Image.new("1", (1, 1))).textlength(message, font=font))

    image = Image.new("1", (text_width + 30, 11))
    draw = ImageDraw.Draw(image)
    draw.text((0, 0), message, font=font, fill=255)

    static_frame = image.crop((0, 0, 30, 11))
    show_frame(static_frame)
    time.sleep(1)

    target_word = "KARRI"
    cutoff = int(ImageDraw.Draw(Image.new("1", (1, 1))).textlength(message.split(target_word)[0], font=font))
    stop_offset = max(0, cutoff)

    prev_pixels = [[0 for _ in range(11)] for _ in range(30)]

    for offset in range(0, stop_offset + 1, step):
        frame = image.crop((offset, 0, offset + 30, 11))
        chips_to_update = set()
        for x in range(30):
            for y in range(11):
                value = frame.getpixel((x, y))
                if prev_pixels[x][y] != value:
                    chip = x // 8
                    x_local = x % 8
                    matrices[chip]._pixel(y, x_local, value)
                    prev_pixels[x][y] = value
                    chips_to_update.add(chip)
        for chip in chips_to_update:
            matrices[chip].show()
        time.sleep(speed)




# --- Flash message ---
def draw_volume_screen(direction, level):
    global prev_pixels

    level = max(0, min(level, 5))
    image = Image.new("1", (30, 11))
    draw = ImageDraw.Draw(image)

    label = "Vol +" if direction == "up" else "Vol -"
    font = ImageFont.truetype("/home/karri/code/Fonts/nothing-font-5x7.otf", size=9)
    draw.text((0, 0), label, font=font, fill=255)

    bar_start_x = 30 - (5 * 2)
    for i in range(level):
        height = i + 1
        x = bar_start_x + i * 2
        for y in range(11 - height, 11):
            if x < 30:
                image.putpixel((x, y), 255)

    # --- Diff + update like channel label ---
    chips_to_update = set()
    for x in range(30):
        for y in range(11):
            value = image.getpixel((x, y))
            if prev_pixels[x][y] != value:
                chip = x // 8
                x_local = x % 8
                matrices[chip]._pixel(y, x_local, value)
                prev_pixels[x][y] = value
                chips_to_update.add(chip)

    [matrices[chip].show() for chip in chips_to_update]


# ——— Initial splash & channel ————————————————————————————————————
clear_all()
scroll_to_karri("Hello, I'm KARRI", speed=0.01, step=3)
time.sleep(2)
clear_all()
sync_prev_pixels()

channels = ["Mum", "Dad", "Sammy", "Tom", "Dance club", "Amy"]
channel_counts = {
    "Mum":        0,
    "Dad":        1,
    "Sammy":      1,
    "Tom":        1,
    "Dance club": 0,
    "Amy": 3
}

current = 0
display_channel_label(channels[current], channel_counts[channels[current]])


# ——— Audio Paths —————————————————
audio_paths = {
    "Mum":   [],
    "Dad":   ["/home/karri/code/Audio Tracks/Dad.mp3"],
    "Sammy": ["/home/karri/code/Audio Tracks/Sam.mp3"],
    "Tom":   ["/home/karri/code/Audio Tracks/Tom.mp3"],
    "Amy": [
        "/home/karri/code/Audio Tracks/Ava3.mp3",
        "/home/karri/code/Audio Tracks/Ava2.mp3",
        "/home/karri/code/Audio Tracks/Ava1.mp3"
    ]
}



# Make sure these state flags are initialized before the loop:
awaiting_send   = False   # True once we’ve shown “Send?”
showing_cancel  = False   # True while we’re in the Cancel screen
prev_confirm_pressed = False

mpg123_proc = None  # playback process handle

# ——— Main loop ———————————————————————————————————————————————————
try:
    while True:
        now = time.time()

        # --- Inactivity check ---
        if time.time() - last_activity_time > INACTIVITY_TIMEOUT:
            clear_all()
            play_idle_animation(idle_animation_frames, duration=5, frame_delay=0.08)
            clear_all()
            sync_prev_pixels()
            display_channel_label(channels[current], channel_counts[channels[current]])
            last_activity_time = time.time()

        if mpg123_proc:
            if mpg123_proc.poll() is not None:
                mpg123_proc = None
                display_channel_label(channels[current], channel_counts[channels[current]])
                last_activity_time = time.time()
            else:
                time.sleep(0.05)
                continue

        if not in_ripple_mode:
            v = y_chan.voltage
            if   v >= RECORD_THRESHOLD:
                y_state = 'record'
            elif v <= CANCEL_THRESHOLD:
                y_state = 'cancel_zone'
            else:
                y_state = 'neutral'

            if not awaiting_send:
                if y_state == 'record' and prev_y_state != 'record':
                    if mpg123_proc:
                        mpg123_proc.terminate()
                        mpg123_proc.wait()
                        mpg123_proc = None
                    display_state_text("Recording")
                    arecord_proc = subprocess.Popen([
                        "arecord", "-D", MIC_DEVICE, "-f", "cd", "-t", "wav", AUDIO_FILE
                    ])
                    last_activity_time = time.time()

                elif y_state != 'record' and prev_y_state == 'record':
                    arecord_proc.terminate()
                    arecord_proc.wait()
                    arecord_proc = None
                    subprocess.run(["aplay", AUDIO_FILE])
                    display_state_text("Send?")
                    awaiting_send = True
                    showing_cancel = False
                    last_activity_time = time.time()

                prev_y_state = y_state

            else:
                if not confirm_button.value:
                    display_state_text("Sent!")
                    time.sleep(1)
                    clear_all()
                    sync_prev_pixels()
                    display_channel_label(channels[current], channel_counts[channels[current]])
                    awaiting_send = False
                    showing_cancel = False
                    last_activity_time = time.time()

                elif y_state == 'cancel_zone' and prev_y_state != 'cancel_zone':
                    display_state_text("Cancel")
                    showing_cancel = True
                    last_activity_time = time.time()

                elif showing_cancel and y_state != 'cancel_zone':
                    clear_all()
                    sync_prev_pixels()
                    display_channel_label(channels[current], channel_counts[channels[current]])
                    awaiting_send = False
                    showing_cancel = False
                    last_activity_time = time.time()

                prev_y_state = y_state

        else:
            if arecord_proc:
                arecord_proc.terminate()
                arecord_proc.wait()
                arecord_proc = None
            prev_y_state = 'neutral'
            awaiting_send = False
            showing_cancel = False

        if not button_up.value:
            start     = time.time()
            triggered = False
            while not button_up.value:
                if time.time() - start >= 3 and not triggered:
                    triggered = True
                    if not in_ripple_mode:
                        circular_ripple()
                        in_ripple_mode = True
                    else:
                        circular_ripple(reverse=True)
                        sync_prev_pixels()
                        in_ripple_mode = False
                        display_channel_label(channels[current], channel_counts[channels[current]])
                    last_activity_time = time.time()
                time.sleep(0.01)

            if not triggered and not in_ripple_mode:
                while True:
                    current = (current + 1) % len(channels)
                    aborted = display_channel_label(
                        channels[current],
                        channel_counts[channels[current]],
                        should_interrupt=lambda: (not button_up.value) or (not button_down.value)
                    )
                    if not aborted:
                        last_activity_time = time.time()
                        break
            time.sleep(0.1)

        if not in_ripple_mode:
            now = time.time()

            if not button_down.value:
                while True:
                    current = (current - 1) % len(channels)
                    aborted = display_channel_label(
                        channels[current], channel_counts[channels[current]],
                        should_interrupt=lambda: (not button_up.value) or (not button_down.value)
                    )
                    if not aborted:
                        last_activity_time = time.time()
                        break
                while not button_down.value:
                    time.sleep(0.01)
                time.sleep(0.1)

            if not volume_up.value and (now - last_volume_button_time) > 0.5:
                volume_level = min(VOLUME_MAX, volume_level + VOLUME_STEP)
                subprocess.run(
                    ["amixer", "sset", "Master", f"{volume_level}%"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                draw_volume_screen("up", volume_level // VOLUME_STEP)
                showing_volume = True
                last_volume_change_time = now
                last_volume_button_time = now
                last_activity_time = time.time()
                time.sleep(0.01)

            if not volume_down.value and (now - last_volume_button_time) > 0.5:
                volume_level = max(VOLUME_MIN, volume_level - VOLUME_STEP)
                subprocess.run(
                    ["amixer", "sset", "Master", f"{volume_level}%"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                draw_volume_screen("down", volume_level // VOLUME_STEP)
                showing_volume = True
                last_volume_change_time = now
                last_volume_button_time = now
                last_activity_time = time.time()
                time.sleep(0.01)

            if showing_volume and (now - last_volume_change_time) > 2:
                display_channel_label(channels[current], channel_counts[channels[current]])
                showing_volume = False

            pressed = not confirm_button.value
            if pressed and not prev_confirm_pressed and not awaiting_send and not showing_cancel and arecord_proc is None:
                tracks = audio_paths.get(channels[current])
                if tracks:
                    for i, track in enumerate(tracks[::-1]):
                        channel_counts[channels[current]] = len(tracks) - i
                        display_channel_label(channels[current], channel_counts[channels[current]])
                        mpg123_proc = subprocess.Popen(["mpg123", track])
                        mpg123_proc.wait()
                        last_activity_time = time.time()

                    channel_counts[channels[current]] = 0
                    display_channel_label(channels[current], channel_counts[channels[current]])
                    last_activity_time = time.time()

            prev_confirm_pressed = pressed

        time.sleep(0.01)


finally:
    # Clean up any in-flight recording
    if arecord_proc:
        arecord_proc.terminate()
        arecord_proc.wait()
    # Clean up any playback
    if mpg123_proc:
        mpg123_proc.terminate()
        mpg123_proc.wait()
    print("Exiting cleanly.")

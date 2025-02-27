import queue
import time
import os
import argparse
import threading
import torch
import numpy as np
import sounddevice as sd
from pydub import AudioSegment, silence
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
from transformers import logging as hf_logging

import cv2
import textwrap
from PIL import Image, ImageDraw, ImageFont
import platform
import logging

# Import Ollama for translation
try:
    from ollama import chat
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False
    logging.warning("Ollama not available. Translation features will be disabled.")

app_version = "0.2.0"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('subtitle_generator')

# Parse command line arguments
parser = argparse.ArgumentParser(description='Real-time subtitle generator with translation')
parser.add_argument('--model', type=str, default="KBLab/kb-whisper-small",
                    help='Whisper model to use (tiny/base/small/medium/large)')
parser.add_argument('--language', type=str, default="sv", 
                    help='Language code for transcription (e.g., sv, en, etc.)')
parser.add_argument('--translate', action='store_true',
                    help='Enable translation of subtitles')
parser.add_argument('--ollama_model', type=str, default="gemma2:9b",
                    help='Ollama model to use for translation (e.g., gemma2:9b, llama3:8b)')
parser.add_argument('--target_language', type=str, default="en",
                    help='Target language for translation (e.g., en, fr, es)')
parser.add_argument('--width', type=int, default=1920, help='Width of output window')
parser.add_argument('--height', type=int, default=1080, help='Height of output window')
parser.add_argument('--fullscreen', action='store_true', help='Run in fullscreen mode')
parser.add_argument('--buffer_size', type=int, default=200, 
                    help='Character buffer size for continuous text')
parser.add_argument('--max_lines', type=int, default=2, help='Maximum lines to display')
parser.add_argument('--chars_per_line', type=int, default=52, 
                    help='Maximum characters per line')
parser.add_argument('--silence_threshold', type=float, default=-40, 
                    help='Silence threshold in dB')
parser.add_argument('--min_silence', type=int, default=400, 
                    help='Minimum silence duration in ms')
parser.add_argument('--save_transcript', action='store_true', 
                    help='Save transcript to a file')
parser.add_argument('--output', type=str, default="transcript.txt", 
                    help='Output file for transcript')
parser.add_argument('--show_original', action='store_true',
                    help='Show original text alongside translation')
args = parser.parse_args()

# Disable translation if Ollama is not available
if args.translate and not OLLAMA_AVAILABLE:
    logger.warning("Translation requested but Ollama is not available. Disabling translation.")
    args.translate = False

#####################################################
# Subtitle Display Configuration
#####################################################

SCREEN_WIDTH = args.width
SCREEN_HEIGHT = args.height
CHROMA_KEY_GREEN = (0, 255, 0)  # BGR for OpenCV
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
RECT_HEIGHT = 300 if args.show_original and args.translate else 220

SUBTITLE_MAX_LINES = args.max_lines
CHARS_PER_LINE = args.chars_per_line
LINE_SPACING = 52
BOTTOM_MARGIN = 130
SENTENCE_BUFFER_SIZE = args.buffer_size

# Adjust for dual display (original + translation)
if args.show_original and args.translate:
    SUBTITLE_MAX_LINES_TOTAL = SUBTITLE_MAX_LINES * 2
    ORIGINAL_COLOR = (200, 200, 200)  # Light gray for original text
    TRANSLATION_COLOR = (255, 255, 255)  # White for translation
else:
    SUBTITLE_MAX_LINES_TOTAL = SUBTITLE_MAX_LINES

# Font selection based on platform
if platform.system() == "Windows":
    FONT_PATH = os.path.join(os.environ["WINDIR"], "Fonts", "Arial.ttf")
elif platform.system() == "Darwin":  # macOS
    FONT_PATH = "/Library/Fonts/Arial Unicode.ttf"
else:  # Linux and others
    # Common locations for fonts on Linux
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
    ]
    FONT_PATH = next((path for path in font_paths if os.path.exists(path)), None)
    if not FONT_PATH:
        logger.warning("No suitable font found. Using default.")
        FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

FONT_SIZE = 52

# State variables
subtitle_lines = []
original_subtitle_buffer = ""
translated_subtitle_buffer = ""
recent_transcriptions = []
sentence_buffer = ""
full_transcript = []
is_paused = False
is_running = True
show_controls = False
mic_level = 0
processing_status = "Ready"
translation_queue = queue.Queue()
translated_texts = {}  # Cache for translated texts

# Create a lock for thread safety when updating shared state
state_lock = threading.Lock()

#####################################################
# Translation Functions
#####################################################

def translation_worker():
    """Worker thread that translates texts from the queue."""
    global translated_texts, processing_status
    
    if not args.translate or not OLLAMA_AVAILABLE:
        return
        
    logger.info(f"Translation worker started with model: {args.ollama_model}")
    
    system_prompt = f"""Act as an expert translator. You have been given a piece of a subtitle. 
Your job is to translate the text from {args.language} to {args.target_language}. 
Respond with ONLY the translated text, nothing else.  
Don't respond, just translate. If you're not given anything to translate or don't understand, 
DON'T give an answer."""
    
    while is_running:
        try:
            # Get text to translate from queue
            text = translation_queue.get(timeout=1.0)
            
            if text is None or text.strip() == "":
                translation_queue.task_done()
                continue
                
            # Skip if already translated
            if text in translated_texts:
                translation_queue.task_done()
                continue
                
            # Update status
            with state_lock:
                processing_status = "Translating..."
                
            # Call Ollama for translation
            try:
                response = chat(
                    model=args.ollama_model,
                    messages=[
                        {
                            'role': 'system',
                            'content': system_prompt
                        },
                        {
                            'role': 'user',
                            'content': text
                        }
                    ]
                )
                
                translated = response.message.content.strip()
                
                # Cache the translation
                with state_lock:
                    translated_texts[text] = translated
                    processing_status = "Ready"
                    
                # Update the displayed translation
                update_translation(text, translated)
                
            except Exception as e:
                logger.error(f"Translation error: {e}")
                with state_lock:
                    processing_status = "Translation error"
                
            translation_queue.task_done()
            
        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Translation worker error: {e}")
            time.sleep(1)

def update_translation(original, translated):
    """Update the translated subtitle buffer with new translation."""
    global translated_subtitle_buffer, original_subtitle_buffer
    
    with state_lock:
        # If showing both, map the corresponding text
        if args.show_original:
            if original.strip() in original_subtitle_buffer:
                # Find corresponding text in original buffer and replace with translation
                translated_subtitle_buffer = translated
        else:
            # Simply update the translation buffer
            translated_subtitle_buffer = translated

#####################################################
# Helper Functions
#####################################################

def get_display_text():
    """Get text for display from the current buffer."""
    with state_lock:
        if args.translate:
            if args.show_original:
                # Return both original and translated text
                original_lines = textwrap.wrap(original_subtitle_buffer, width=CHARS_PER_LINE)
                translated_lines = textwrap.wrap(translated_subtitle_buffer, width=CHARS_PER_LINE)
                
                # Ensure we don't exceed max lines for each
                original_lines = original_lines[-SUBTITLE_MAX_LINES:] if original_lines else []
                translated_lines = translated_lines[-SUBTITLE_MAX_LINES:] if translated_lines else []
                
                # Format: original lines first, then a blank line, then translated lines
                display_lines = original_lines + [""] + translated_lines
                return display_lines[-SUBTITLE_MAX_LINES_TOTAL:] if display_lines else []
            else:
                # Just show translated text
                lines = textwrap.wrap(translated_subtitle_buffer, width=CHARS_PER_LINE)
                return lines[-SUBTITLE_MAX_LINES:] if lines else []
        else:
            # Regular text display (no translation)
            lines = textwrap.wrap(sentence_buffer, width=CHARS_PER_LINE)
            return lines[-SUBTITLE_MAX_LINES:] if lines else []

def add_subtitle_text(new_text):
    """
    Improved function that maintains a continuous buffer of recent speech
    and intelligently breaks it into visible lines.
    """
    global sentence_buffer, full_transcript, original_subtitle_buffer
    
    with state_lock:
        # Add new text to the full transcript
        full_transcript.append(new_text.strip())
        
        if args.translate:
            # Update original text buffer for translation
            original_subtitle_buffer = new_text.strip()
            
            # Queue for translation
            translation_queue.put(new_text.strip())
        else:
            # Regular subtitle processing (no translation)
            # Add new text to our sentence buffer
            sentence_buffer += " " + new_text.strip()
            
            # Keep only the most recent portion of speech (last N characters)
            if len(sentence_buffer) > SENTENCE_BUFFER_SIZE:
                # Find a good break point (period, question mark, etc.) if possible
                good_break_points = ['.', '!', '?', ';']
                break_points = []
                
                cutoff_start = len(sentence_buffer) - SENTENCE_BUFFER_SIZE
                for char in good_break_points:
                    positions = [pos for pos in range(cutoff_start, len(sentence_buffer)) 
                                 if sentence_buffer[pos] == char]
                    break_points.extend(positions)
                    
                if break_points:
                    # Use the latest good break point that's in the early part of the buffer
                    cutoff = max(min(break_points) + 1, cutoff_start)
                    sentence_buffer = sentence_buffer[cutoff:].strip()
                else:
                    # If no good break point, try to break at a word boundary
                    text_to_keep = sentence_buffer[cutoff_start:]
                    first_space = text_to_keep.find(" ")
                    if first_space > 0:
                        sentence_buffer = text_to_keep[first_space:].strip()
                    else:
                        # If no word boundary, just keep the last N characters
                        sentence_buffer = sentence_buffer[-SENTENCE_BUFFER_SIZE:].strip()


def create_subtitle_frame():
    global mic_level, processing_status, show_controls
    
    # 1) Create the image in green
    pil_image = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), (0, 255, 0))
    draw = ImageDraw.Draw(pil_image)

    # 2) Draw black rectangle at bottom
    black_rect_top = SCREEN_HEIGHT - RECT_HEIGHT
    draw.rectangle([(0, black_rect_top), (SCREEN_WIDTH, SCREEN_HEIGHT)], fill=(0, 0, 0))

    # 3) Set up font & margins
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except Exception as e:
        logger.error(f"Font error: {e}")
        # Fallback to default font
        font = ImageFont.load_default()
    
    y_start = SCREEN_HEIGHT - BOTTOM_MARGIN

    # 4) Get current text to display
    lines = get_display_text()

    # 5) Draw each line from bottom to top
    for i in range(len(lines)):
        text = lines[len(lines) - 1 - i]
        y = y_start - i * LINE_SPACING

        # Skip empty separator line
        if not text.strip():
            continue

        # Use textbbox to measure text width/height
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        text_width = right - left

        # Center horizontally
        x = (SCREEN_WIDTH - text_width) // 2

        # Determine text color based on whether it's original or translated
        text_color = WHITE
        if args.translate and args.show_original:
            # If showing both original and translation, use different colors
            if i >= len(lines) - SUBTITLE_MAX_LINES:
                text_color = TRANSLATION_COLOR  # Translation (white)
            else:
                text_color = ORIGINAL_COLOR  # Original (light gray)

        draw.text((x, y), text, font=font, fill=text_color)

    # 6) Add status overlay if controls are showing
    if show_controls:
        # Draw semi-transparent overlay at top
        overlay_height = 80
        draw.rectangle([(0, 0), (SCREEN_WIDTH, overlay_height)], 
                      fill=(0, 0, 0, 180))
        
        # Show mic level
        level_width = int(mic_level * 200)  # Scale to 200px max
        draw.rectangle([(20, 20), (20 + level_width, 40)], fill=(0, 255, 0))
        draw.rectangle([(20, 20), (220, 40)], outline=(255, 255, 255))
        
        # Show status
        status_text = f"Status: {processing_status}"
        if is_paused:
            status_text += " (PAUSED)"
        if args.translate:
            status_text += f" | Translation: {args.language} → {args.target_language}"
        draw.text((250, 20), status_text, font=font, fill=(255, 255, 255))
        
        # Show help text
        help_text = "P: Pause | S: Save | ESC: Exit | H: Hide Controls"
        draw.text((20, 50), help_text, font=font, fill=(255, 255, 255))

    # 7) Convert Pillow (RGB) -> NumPy (BGR) for OpenCV
    open_cv_image = np.array(pil_image)[:, :, ::-1].copy()
    return open_cv_image


def save_transcript():
    """Save the full transcript to a file."""
    with state_lock:
        if not full_transcript:
            return False
        
        with open(args.output, 'w', encoding='utf-8') as f:
            for text in full_transcript:
                f.write(text + '\n')
                # Add translation if available
                if args.translate and text in translated_texts:
                    f.write(f"[{args.target_language.upper()}]: {translated_texts[text]}\n\n")
                else:
                    f.write('\n')
        return True


def is_chunk_silent(chunk, threshold_db=-35):
    """Check if an audio chunk is silent based on dB threshold."""
    try:
        return chunk.dBFS < threshold_db
    except AttributeError:
        # Handle case where chunk doesn't have dBFS attribute
        return chunk.max_dBFS < threshold_db


def update_mic_level(audio_chunk):
    """Update the microphone level indicator from audio chunk."""
    global mic_level
    try:
        # Calculate normalized mic level (0-1)
        db_level = audio_chunk.dBFS
        # Map from typical dB range (-60 to 0) to 0-1
        norm_level = max(0, min(1, (db_level + 60) / 60))
        with state_lock:
            mic_level = norm_level
    except Exception:
        pass  # Ignore errors in level calculation


#####################################################
# Setup Device and Load Model
#####################################################

def setup_model():
    """Set up and load the transcription model."""
    global processing_status
    
    with state_lock:
        processing_status = "Loading model..."
    
    hf_logging.set_verbosity_error()
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    model_id = args.model

    logger.info(f"Loading model {model_id} on {device}...")
    
    try:
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_id,
            torch_dtype=torch_dtype,
            use_safetensors=True,
            cache_dir="cache"
        )
        model.to(device)

        processor = AutoProcessor.from_pretrained(model_id)

        asr_pipe = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            torch_dtype=torch_dtype,
            device=device,
        )
        
        with state_lock:
            processing_status = "Ready"
        
        return asr_pipe
    except Exception as e:
        logger.error(f"Error loading model: {e}")
        with state_lock:
            processing_status = f"Error: {str(e)[:30]}..."
        raise


def transcribe_chunk(chunk, asr_pipe):
    """Transcribe an audio chunk using the provided ASR pipeline."""
    with state_lock:
        processing_status = "Transcribing..."
    
    try:
        raw_samples = np.array(chunk.get_array_of_samples()).astype(np.float32)
        raw_samples /= 32767.0  # Normalize to [-1.0, 1.0]

        audio_input = {"array": raw_samples, "sampling_rate": 16000}
        result = asr_pipe(
            audio_input,
            chunk_length_s=30,
            generate_kwargs={"task": "transcribe", "language": args.language}
        )
        
        with state_lock:
            processing_status = "Ready"
        
        return result["text"]
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        with state_lock:
            processing_status = "Error in transcription"
        return ""


def transcribe_if_not_silent(chunk, asr_pipe, threshold_db=-35):
    """Transcribe a chunk only if it's not silent."""
    if is_chunk_silent(chunk, threshold_db=threshold_db):
        return ""
    else:
        return transcribe_chunk(chunk, asr_pipe)


#####################################################
# Audio Processing Thread
#####################################################

def audio_processing_thread(audio_queue, asr_pipe):
    global is_running, is_paused, processing_status
    
    buffered_segment = AudioSegment.empty()
    silence_start_time = None
    
    # Words that may be falsely detected during silence
    excluded_texts = set(["Tack.", "Tack!", "Ja.", "Musik"])
    
    # For different languages, you might need different exclusions
    if args.language != "sv":
        excluded_texts = set(["Thanks.", "Thank you!", "Yes.", "Music", "Um.", "Uh."])
    
    logger.info("Audio processing thread started")
    
    try:
        while is_running:
            if is_paused:
                time.sleep(0.1)
                continue
                
            # 1) If we have data
            if not audio_queue.empty():
                try:
                    data_block = audio_queue.get(timeout=0.1)
                    block_int16 = (data_block * 32767).astype(np.int16).tobytes()

                    segment = AudioSegment(
                        data=block_int16,
                        sample_width=2,
                        frame_rate=16000,
                        channels=1
                    )
                    buffered_segment += segment
                    
                    # Update mic level indicator
                    update_mic_level(segment)

                    # 2) Silence splitting with adaptive parameters
                    chunks = silence.split_on_silence(
                        buffered_segment,
                        min_silence_len=args.min_silence,
                        silence_thresh=args.silence_threshold,
                        keep_silence=100
                    )

                    if len(chunks) > 1:
                        finished_chunks = chunks[:-1]
                        for c in finished_chunks:
                            if not is_running:
                                break
                                
                            text = transcribe_chunk(c, asr_pipe)
                            if text.strip() and text.strip() not in excluded_texts:
                                logger.info(f"Transcribed: {text}")
                                
                                # Add to recent transcriptions and subtitle
                                with state_lock:
                                    recent_transcriptions.append(text)
                                    if len(recent_transcriptions) > 5:
                                        recent_transcriptions.pop(0)
                                        
                                add_subtitle_text(text)
                        buffered_segment = chunks[-1]
                        silence_start_time = None  # Reset silence timer

                    # 3) Forced chunk if buffer is too long
                    if len(buffered_segment) > 8000:
                        text = transcribe_if_not_silent(buffered_segment, asr_pipe, 
                                                       args.silence_threshold)
                        if text.strip() and text.strip() not in excluded_texts:
                            logger.info(f"Transcribed (forced): {text}")
                            add_subtitle_text(text)
                        buffered_segment = AudioSegment.empty()
                        silence_start_time = None  # Reset silence timer
                        
                except queue.Empty:
                    pass
                except Exception as e:
                    logger.error(f"Error processing audio: {e}")
                    with state_lock:
                        processing_status = "Error processing audio"
            else:
                # Check for silence duration
                if silence_start_time is None:
                    silence_start_time = time.time()
                elif time.time() - silence_start_time > args.min_silence / 1000.0:
                    # Force transcription if silence duration exceeds threshold
                    text = transcribe_if_not_silent(buffered_segment, asr_pipe, 
                                                    args.silence_threshold)
                    if text.strip() and text.strip() not in excluded_texts:
                        logger.info(f"Transcribed (silence): {text}")
                        add_subtitle_text(text)
                    buffered_segment = AudioSegment.empty()
                    silence_start_time = None  # Reset silence timer
                
                time.sleep(0.01)
                
    except Exception as e:
        logger.error(f"Audio processing thread error: {e}")
    finally:
        logger.info("Audio processing thread ending")


#####################################################
# Main Function
#####################################################

def main():
    global is_running, is_paused, show_controls
    
    logger.info(f"Starting Subtitle Generator v{app_version}")
    if args.translate:
        logger.info(f"Translation enabled: {args.language} → {args.target_language} using model {args.ollama_model}")
    
    # Start translation worker if needed
    if args.translate and OLLAMA_AVAILABLE:
        translation_thread = threading.Thread(
            target=translation_worker,
            daemon=True
        )
        translation_thread.start()
    
    # Load the ASR model
    try:
        asr_pipe = setup_model()
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        return
    
    # Setup audio queue and callback
    sample_rate = 16000
    block_size_ms = 200
    block_size = int(sample_rate * (block_size_ms / 1000))
    audio_queue = queue.Queue()

    def audio_callback(indata, frames, time_info, status):
        if status:
            logger.warning(f"Audio status: {status}")
        audio_queue.put(indata.copy())

    # Start audio stream
    try:
        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            blocksize=block_size,
            callback=audio_callback
        )
        stream.start()
    except Exception as e:
        logger.error(f"Audio stream error: {e}")
        return
    
    # Start processing thread
    processing_thread = threading.Thread(
        target=audio_processing_thread,
        args=(audio_queue, asr_pipe),
        daemon=True
    )
    processing_thread.start()
    
    # Setup display window
    cv2.namedWindow("Subtitles", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Subtitles", SCREEN_WIDTH, SCREEN_HEIGHT)
    
    if args.fullscreen:
        cv2.setWindowProperty("Subtitles", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    
    logger.info("Starting subtitle display. Press ESC to exit, P to pause/resume, H to show/hide controls.")
    
    try:
        while is_running:
            # Create and display the subtitle frame
            frame = create_subtitle_frame()
            cv2.imshow("Subtitles", frame)
            
            # Process key presses
            key = cv2.waitKey(10)
            if key == 27:  # ESC to exit
                is_running = False
            elif key == ord('p') or key == ord('P'):  # P to pause/resume
                is_paused = not is_paused
                logger.info(f"{'Paused' if is_paused else 'Resumed'} transcription")
            elif key == ord('h') or key == ord('H'):  # H to show/hide controls
                show_controls = not show_controls
            elif key == ord('s') or key == ord('S'):  # S to save transcript
                if args.save_transcript or save_transcript():
                    logger.info(f"Saved transcript to {args.output}")
                    with state_lock:
                        processing_status = f"Saved to {args.output}"
                else:
                    logger.warning("No transcript to save")
            
            time.sleep(0.01)
                
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
    finally:
        # Cleanup resources
        is_running = False
        stream.stop()
        stream.close()
        cv2.destroyAllWindows()
        
        # Wait for processing thread to end
        processing_thread.join(timeout=1.0)
        
        # Save transcript if enabled
        if args.save_transcript:
            save_transcript()
            logger.info(f"Saved transcript to {args.output}")
        
        logger.info("Subtitle generator shutdown complete")


if __name__ == "__main__":
    main()
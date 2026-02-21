# Usage Guide: Voice Transcription

Nexus Core now includes support for voice-to-text transcription using the `WhisperTranscriptionProvider`. This allows you to easily convert audio files into text.

## Features

- **Whisper Integration**: Utilizes OpenAI's Whisper model for high-quality speech-to-text transcription.
- **Multiple Audio Formats**: Supports a variety of audio formats, including MP3, MP4, MPEG, MPGA, M4A, WAV, WEBM, OGG, and OGG_VORBIS.
- **Adapter Registry**: Seamlessly integrated with the `AdapterRegistry` for easy instantiation and management of transcription providers.

## How to Use

To use the voice transcription feature, you will typically:

1.  **Instantiate the Transcription Provider**: Obtain an instance of `WhisperTranscriptionProvider` through the `AdapterRegistry`.
2.  **Prepare Audio Input**: Provide the audio source as either a file path (local or URL) or raw bytes.
3.  **Transcribe**: Call the `transcribe` method on the provider instance.

### Example: Transcribing a Local Audio File

```python
import asyncio
from pathlib import Path
from nexus.core.adapters import AdapterRegistry
from nexus.adapters.transcription import TranscriptionInput

async def main():
    # 1. Instantiate the Transcription Provider
    #    The 'whisper' provider is registered by default.
    registry = AdapterRegistry()
    whisper_provider = registry.create_transcription("whisper")

    # 2. Prepare Audio Input
    audio_file_path = Path("./temp_voice.ogg") # Replace with your audio file path

    # For a URL: audio_file_path = "https://example.com/audio.ogg"

    # Create a TranscriptionInput object
    audio_input = TranscriptionInput(source=audio_file_path, format="ogg") # Specify format

    # 3. Transcribe the audio
    print(f"Transcribing {audio_file_path}...")
    try:
        transcription_result = await whisper_provider.transcribe(audio_input)
        print("
Transcription Result:")
        print(f"Text: {transcription_result.text}")
        print(f"Language: {transcription_result.language}")
        # print(f"Segments: {transcription_result.segments}") # Uncomment for segment details
    except Exception as e:
        print(f"Error during transcription: {e}")

if __name__ == "__main__":
    asyncio.run(main())
```

### Supported Formats

The `WhisperTranscriptionProvider` supports the following audio formats:

-   `mp3`
-   `mp4`
-   `mpeg`
-   `mpga`
-   `m4a`
-   `wav`
-   `webm`
-   `ogg`
-   `ogg_vorbis`

Ensure you specify the correct format when creating `TranscriptionInput`.

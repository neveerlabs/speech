import shutil
import subprocess


class AudioCaptureError(RuntimeError):
    pass


def resolve_monitor_source(default_sink: str, sources_output: str) -> str:
    sources = {
        fields[1]
        for line in sources_output.splitlines()
        if len(fields := line.split()) >= 2
    }
    monitor_source = f"{default_sink}.monitor"
    if monitor_source in sources:
        return monitor_source

    raise AudioCaptureError(
        "Monitor audio output default tidak ditemukan. Pastikan PipeWire/PulseAudio "
        "aktif dan sink output memiliki monitor source."
    )


def get_monitor_source() -> str:
    if not shutil.which("pactl"):
        raise AudioCaptureError(
            "Perintah pactl tidak ditemukan. Pasang paket pulseaudio-utils."
        )

    try:
        default_sink_result = subprocess.run(
            ["pactl", "get-default-sink"],
            check=True,
            capture_output=True,
            text=True,
        )
        sources_result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        details = error.stderr.strip() or error.stdout.strip()
        raise AudioCaptureError(
            f"Gagal membaca perangkat audio sistem: {details or error}"
        ) from error

    return resolve_monitor_source(
        default_sink_result.stdout.strip(), sources_result.stdout
    )

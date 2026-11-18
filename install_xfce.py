import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DESKTOP_FILENAME = "gemini-live-subtitles.desktop"


def _desktop_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_desktop_entry(python_executable: Path, app_path: Path) -> str:
    command = (
        f"{_desktop_quote(str(python_executable))} "
        f"{_desktop_quote(str(app_path))} --tray"
    )
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=Subtitle Gemini Live",
            "Comment=Terjemahan audio sistem ke subtitle Bahasa Indonesia",
            f"Exec={command}",
            "Icon=audio-headphones",
            "Terminal=false",
            "StartupNotify=false",
            "X-GNOME-Autostart-enabled=true",
            "OnlyShowIn=XFCE;",
            "Categories=AudioVideo;Utility;",
            "",
        ]
    )


def install() -> None:
    python_executable = PROJECT_ROOT / ".venv" / "bin" / "python"
    app_path = PROJECT_ROOT / "app.py"
    if not python_executable.is_file():
        raise FileNotFoundError(
            f"Python virtualenv tidak ditemukan: {python_executable}. "
            "Buat .venv dan pasang requirements.txt terlebih dahulu."
        )

    entry = build_desktop_entry(python_executable, app_path)
    destinations = (
        Path.home() / ".config" / "autostart" / DESKTOP_FILENAME,
        Path.home()
        / ".local"
        / "share"
        / "applications"
        / DESKTOP_FILENAME,
    )
    for destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(entry, encoding="utf-8")
    print("Autostart dan launcher XFCE berhasil dipasang.")
    print("Logout/login, lalu klik ikon speaker di area tray untuk mengaktifkan.")


def remove() -> None:
    app_path = str(PROJECT_ROOT / "app.py")
    destinations = (
        Path.home() / ".config" / "autostart" / DESKTOP_FILENAME,
        Path.home()
        / ".local"
        / "share"
        / "applications"
        / DESKTOP_FILENAME,
    )
    for destination in destinations:
        if destination.exists():
            if app_path not in destination.read_text(encoding="utf-8"):
                raise RuntimeError(
                    f"File tidak dibuat oleh instalasi ini, tidak dihapus: {destination}"
                )
            destination.unlink()
    print("Autostart dan launcher XFCE dihapus.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pasang atau hapus tray/autostart Subtitle Gemini di XFCE"
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Hapus entri autostart dan launcher yang dibuat oleh skrip ini",
    )
    arguments = parser.parse_args()
    try:
        remove() if arguments.remove else install()
    except (FileNotFoundError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

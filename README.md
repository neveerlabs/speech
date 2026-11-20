# Speech AI translator

Aplikasi desktop untuk menangkap audio keluaran sistem di Kali Linux/XFCE, menerjemahkannya ke Bahasa Indonesia menggunakan Gemini Live Translation, lalu menampilkan hasilnya sebagai subtitle overlay

## Fitur

- Menerjemahkan audio dari aplikasi desktop secara streaming
- Menampilkan subtitle Bahasa Indonesia sebagai overlay di atas aplikasi lain
- Menyediakan ikon tray XFCE untuk dapat menggunakan tanpa perlu membuka terminal
- Dapat dipasang agar berjalan otomatis saat login XFCE
- Menerjemahkan output suara dari audio yg terdengar kedalam konteks bahasa indonesia

## Kebutuhan

- **Kali Linux** dengan desktop environrment **`XFCE`**
- **Python 3.13** atau yang lebih baru
- **PipeWire** dengan kompatibilitas **PulseAudio** aktif (`pipewire-pulse`)
- Command `pactl` dan `parec`, tersedia dari package `pulseaudio-utils`
- **Gemini API** key dengan akses ke **Gemini Live API**

**Install kebutuhan system:**

```bash
sudo apt install pipewire-pulse pulseaudio-utils
```

## Cara penggunaan

### 1. Clone project dan siapkan API key

Clone repo terlebih dahulu:

```bash
git clone https://github.com/neveerlabs/speech.git
cd speech
python3 -m pip install -r requirements.txt
cp -n .env.example .env
chmod 600 .env
```

Edit `.env` lalu masukkan satu atau beberapa API key, pisahkan dengan koma:

```dotenv
GEMINI_API_KEYS=API_KEY_PERTAMA,API_KEY_KEDUA
```

### 2. Jalankan aplikasi

Untuk penggunaan dari terminal:

```bash
python3 app.py
```
> *Tekan `Ctrl+C` untuk menghentikan aplikasi*

Untuk menjalankan aplikasi dari toggle mode:

```bash
python3 app.py --tray
```
> *Klik kiri ikon tray untuk mulai atau menghentikan terjemahan. Klik kanan ikon tray, pilih **Tampilkan panel** untuk membuka panel chat (akan berubah menjadi **Tutup panel** saat panel terbuka), atau **close** untuk menutup aplikasi.*

### 3. Opsional: jalankan otomatis saat login XFCE

Setelah menginstall requirements, jalankan:

```bash
python3 install_xfce.py
```
> Restart desktop, tray icon speech akan muncul dipanel desktop; klik ikonnya untuk mulai menerjemahkan. Pastikan panel XFCE memiliki item **Notification Area** atau **Status Notifier**

Untuk menghapus autostart dan launcher yang telah dipasang oleh aplikasi:

```bash
python3 install_xfce.py --remove
```

## Catatan

- Terjemahan audio desktop tetap dikirim streaming terpisah dari fitur chat.
- Panel chat terhubung langsung ke Gemini (`gemini-3.5-flash-lite`, thinking level minimal) tanpa RAG. Riwayat lokal memakai SQLite di `$XDG_DATA_HOME/speech-ai/speech_ai.sqlite3` (default: `~/.local/share/speech-ai/speech_ai.sqlite3`); seluruh pesan tersimpan dikirim sebagai konteks pada setiap permintaan. Balasan Gemini diterima dengan streaming API, ditampilkan hingga 32 karakter setiap 20 ms, dan langsung dirender sebagai Markdown; bila request gagal dan berpindah key, teks sementara akan direset. Pesan user dan balasan AI ditampilkan dengan Markdown bergaya GitHub (heading, list berindentasi ringkas, kutipan, kode, tautan, tabel, task list, dan format teks lainnya); balasan AI diminta hanya menggunakan Markdown, bukan HTML. Code fence diberi warna sintaks sesuai bahasa (termasuk Bash), label bahasa, gutter nomor baris yang menyesuaikan jumlah digit, dan panduan indentasi; blok panjang dibatasi 10 baris tampak dan dapat digulir. Tombol salin kode selalu terlihat tanpa border; setelah diklik, ikon berubah menjadi centang abu-abu sementara dan toast "Code copied" muncul. Tombol salin pesan di pesan user maupun balasan AI menyalin isi pesan dan menampilkan toast "Message copied".
- Pesan contoh tidak ditampilkan saat panel chat dibuka.
- Jika koneksi Gemini gagal, API key berikutnya dicoba. Error dari capture audio tidak memicu pergantian API! `GEMINI_API_KEY` tunggal juga masih didukung untuk kompatibilitas
- Aplikasi memakai monitor source dari sink audio default dan model `gemini-3.5-live-translate-preview`
- Audio dikirim dalam potongan 100 ms, kecepatan subtitle tetap bergantung pada model dan jaringan, sehingga teks tidak selalu muncul tepat bersamaan dengan ucapan
- Aplikasi menangkap audio yang sedang diputar dari desktop secara langsung, bukan suara dari mikrofon (dalam tahap pengembangan awal)
- Mendukung beberapa Gemini API key yang dicoba berurutan jika sesi atau koneksi gagal (sebagai feedback penanganan error)
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
python -m pip install -r requirements.txt
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
python app.py
```
> *Tekan `Ctrl+C` untuk menghentikan aplikasi*

Untuk menjalankan aplikasi dari toggle mode:

```bash
python app.py --tray
```
> *Klik tray icon speech lalu klik sekali untuk memulai, klik lagi untuk berhenti; klik kiri di toggle mode, pilih **Keluar** untuk menutup aplikasi*

### 3. Opsional: jalankan otomatis saat login XFCE

Setelah menginstall requirements, jalankan:

```bash
python install_xfce.py
```
> Restart desktop, tray icon speech akan muncul dipanel desktop; klik ikonnya untuk mulai menerjemahkan. Pastikan panel XFCE memiliki item **Notification Area** atau **Status Notifier**

Untuk menghapus autostart dan launcher yang telah dipasang oleh aplikasi:

```bash
python install_xfce.py --remove
```

## Catatan

- Saat ini, masih hanya audio saja yg dikirim secara streaming ke layanan Gemini, untuk bagian input belom
- Jika koneksi Gemini gagal, API key berikutnya dicoba. Error dari capture audio tidak memicu pergantian API! `GEMINI_API_KEY` tunggal juga masih didukung untuk kompatibilitas
- Aplikasi memakai monitor source dari sink audio default dan model `gemini-3.5-live-translate-preview`
- Audio dikirim dalam potongan 100 ms, kecepatan subtitle tetap bergantung pada model dan jaringan, sehingga teks tidak selalu muncul tepat bersamaan dengan ucapan
- Aplikasi menangkap audio yang sedang diputar dari desktop secara langsung, bukan suara dari mikrofon (dalam tahap pengembangan awal)
- Mendukung beberapa Gemini API key yang dicoba berurutan jika sesi atau koneksi gagal (sebagai feedback penanganan error)
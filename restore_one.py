#!/usr/bin/env python3
"""
Antigravity Restore One Conversation
=====================================
Menampilkan semua conversation yang ada di disk (terbaru dulu),
lalu user memilih satu untuk di-restore ke dalam database index
tanpa mengganggu conversation lain yang sudah ada.

Disesuaikan khusus untuk folder:
  - Sumber percakapan : .gemini/antigravity-ide/conversations
  - Sumber brain      : .gemini/antigravity-ide/brain
  - Target database   : Antigravity IDE (state.vscdb)
(Folder versi lama 'antigravity' dan 'antigravity-backup' diabaikan)

Mendukung update upstream v1.06 & v1.07:
  - Format .db (SQLite) & .pb (legacy)
  - Ekstraksi judul bermakna dari prompt user pertama di file .db
  - Recovery judul dari transcript brain
  - Penyegaran timestamp update & pembersihan duplikat
  - Backup aman dengan timestamp unik

Requirements: Python 3.7+
"""

import sys
import os

if sys.version_info[0] < 3:
    try:
        sys.stdout.flush()
        os.execvp("python3", ["python3"] + sys.argv)
    except OSError:
        sys.stderr.write("ERROR: Python 3.7+ dibutuhkan.\n")
        sys.exit(1)

if sys.version_info < (3, 7):
    sys.stderr.write("ERROR: Python 3.7+ dibutuhkan.\n")
    sys.exit(1)

import sqlite3
import base64
import json
import re
import time
import platform
import subprocess
from urllib.parse import unquote

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---- Ambil fungsi-fungsi inti dari rebuild_conversations.py ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_REBUILD = os.path.join(_HERE, "rebuild_conversations.py")

if not os.path.exists(_REBUILD):
    print("ERROR: rebuild_conversations.py tidak ditemukan di folder yang sama.")
    print(f"       Pastikan restore_one.py dan rebuild_conversations.py")
    print(f"       ada di folder yang sama: {_HERE}")
    input("\nTekan Enter untuk keluar...")
    sys.exit(1)

import importlib.util
spec = importlib.util.spec_from_file_location("rebuild_conversations", _REBUILD)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

# ---- Deteksi Path Khusus Antigravity IDE ----

def get_ide_specific_paths():
    """
    Mengambil path khusus Antigravity IDE (mengabaikan versi lama/backup).
    Returns (conv_dir, brain_dir, db_paths)
    """
    if rc._SYSTEM == "Windows":
        profile = os.path.expandvars(r"%USERPROFILE%")
        appdata = os.path.expandvars(r"%APPDATA%")
        gemini = os.path.join(profile, ".gemini")

        conv_dir = os.path.join(gemini, "antigravity-ide", "conversations")
        brain_dir = os.path.join(gemini, "antigravity-ide", "brain")
        db_candidates = [
            os.path.join(appdata, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
        ]
    elif rc._IS_WSL:
        win_appdata = rc._get_wsl_windows_appdata()
        home = os.path.expanduser("~")
        conv_dir = os.path.join(home, ".gemini", "antigravity-ide", "conversations")
        brain_dir = os.path.join(home, ".gemini", "antigravity-ide", "brain")
        db_candidates = [
            os.path.join(win_appdata, "Antigravity IDE", "User", "globalStorage", "state.vscdb")
        ] if win_appdata else []
    elif rc._SYSTEM == "Darwin":
        home = os.path.expanduser("~")
        support = os.path.join(home, "Library", "Application Support")
        conv_dir = os.path.join(home, ".gemini", "antigravity-ide", "conversations")
        brain_dir = os.path.join(home, ".gemini", "antigravity-ide", "brain")
        db_candidates = [
            os.path.join(support, "Antigravity IDE", "User", "globalStorage", "state.vscdb")
        ]
    else:  # Linux
        home = os.path.expanduser("~")
        config = os.path.join(home, ".config")
        conv_dir = os.path.join(home, ".gemini", "antigravity-ide", "conversations")
        brain_dir = os.path.join(home, ".gemini", "antigravity-ide", "brain")
        db_candidates = [
            os.path.join(config, "Antigravity IDE", "User", "globalStorage", "state.vscdb")
        ]

    existing_dbs = [p for p in db_candidates if os.path.exists(p)]
    # Jika database Antigravity IDE tidak ditemukan spesifik, fallback ke deteksi umum rc
    if not existing_dbs:
        existing_dbs = rc.DB_PATHS

    return conv_dir, brain_dir, existing_dbs


# ---- Fungsi-fungsi Inti ----

def list_all_conversations(conv_dir=None):
    """
    Mengambil katalog percakapan khusus folder antigravity-ide.
    Deduplikasi ID — format .db diprioritaskan atas .pb.
    Diurutkan berdasarkan waktu modifikasi terbaru.
    """
    if conv_dir is None:
        conv_dir, _, _ = get_ide_specific_paths()

    if not os.path.isdir(conv_dir):
        return []

    catalog = {}
    try:
        for name in sorted(os.listdir(conv_dir)):
            lower = name.lower()
            if lower.endswith(".db") and not lower.endswith((".db-shm", ".db-wal")):
                cid = name[:-3]
                catalog[cid] = os.path.join(conv_dir, name)
            elif lower.endswith(".pb"):
                cid = name[:-3]
                if cid not in catalog:
                    catalog[cid] = os.path.join(conv_dir, name)
    except Exception:
        pass

    sorted_convs = sorted(
        catalog.items(),
        key=lambda x: os.path.getmtime(x[1]),
        reverse=True
    )
    return [(cid, path, os.path.getmtime(path)) for cid, path in sorted_convs]


def get_conversation_already_in_db(db_paths):
    """
    Membaca semua conversation_id yang sudah terdaftar di database Antigravity IDE.
    """
    indexed = set()
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM ItemTable "
                "WHERE key='antigravityUnifiedStateSync.trajectorySummaries'"
            )
            row = cur.fetchone()
            conn.close()
            if not row or not row[0]:
                continue

            decoded = base64.b64decode(row[0])
            pos = 0
            while pos < len(decoded):
                tag, pos = rc.decode_varint(decoded, pos)
                wire_type = tag & 7
                if wire_type != 2:
                    break
                length, pos = rc.decode_varint(decoded, pos)
                entry = decoded[pos:pos + length]
                pos += length

                ep = 0
                while ep < len(entry):
                    t, ep = rc.decode_varint(entry, ep)
                    fn, wt = t >> 3, t & 7
                    if wt == 2:
                        l, ep = rc.decode_varint(entry, ep)
                        content = entry[ep:ep + l]
                        ep += l
                        if fn == 1:
                            cid = content.decode('utf-8', errors='replace')
                            indexed.add(cid)
                    elif wt == 0:
                        _, ep = rc.decode_varint(entry, ep)
                    elif wt == 1:
                        ep += 8
                    elif wt == 5:
                        ep += 4
                    else:
                        break
        except Exception:
            pass
    return indexed


def remove_entry_by_cid(protobuf_bytes, target_cid):
    """
    Menghapus entri percakapan lama untuk target_cid dari raw bytes protobuf,
    sehingga saat di-restore ulang tidak terjadi duplikasi entri di database.
    """
    cleaned = b""
    pos = 0
    while pos < len(protobuf_bytes):
        entry_start = pos
        tag, pos = rc.decode_varint(protobuf_bytes, pos)
        wire_type = tag & 7
        if wire_type != 2:
            cleaned += protobuf_bytes[entry_start:]
            break
        length, pos = rc.decode_varint(protobuf_bytes, pos)
        entry_data = protobuf_bytes[pos:pos + length]
        entry_end = pos + length
        pos = entry_end

        # Cek apakah field 1 (conversation_id) cocok dengan target_cid
        cid = None
        ep = 0
        while ep < len(entry_data):
            t, ep = rc.decode_varint(entry_data, ep)
            fn, wt = t >> 3, t & 7
            if wt == 2:
                l, ep = rc.decode_varint(entry_data, ep)
                val = entry_data[ep:ep + l]
                ep += l
                if fn == 1:
                    cid = val.decode('utf-8', errors='replace')
                    break
            elif wt == 0:
                _, ep = rc.decode_varint(entry_data, ep)
            elif wt == 1:
                ep += 8
            elif wt == 5:
                ep += 4
            else:
                break

        if cid != target_cid:
            cleaned += protobuf_bytes[entry_start:entry_end]

    return cleaned


def clean_workspace_path(p):
    """
    Normalisasi string workspace path agar bersih dari format file:/// 
    dan bebas dari karakter binary/URL-encoded.
    """
    if not p:
        return ""
    p_unq = unquote(str(p))
    m_file = re.search(r'file:/*([a-zA-Z]:/[^\r\n\t\x00-\x1f\x7f-\xff\"\'\<\>]+)', p_unq)
    if m_file:
        return os.path.normpath(m_file.group(1).rstrip('/\\'))
    m_direct = re.search(r'([a-zA-Z]:[\\/][^\r\n\t\x00-\x1f\x7f-\xff\"\'\<\>]+)', p_unq)
    if m_direct:
        return os.path.normpath(m_direct.group(1).rstrip('/\\'))
    if rc._SYSTEM != "Windows":
        m_unix = re.search(r'file:/*(/[^\r\n\t\x00-\x1f\x7f-\xff\"\'\<\>]+)', p_unq)
        if m_unix:
            return os.path.normpath(m_unix.group(1).rstrip('/\\'))
        if p_unq.startswith('/'):
            return os.path.normpath(p_unq.strip())
    if "vscode-remote://" in p_unq:
        m_remote = re.search(r'(vscode-remote://[^\r\n\t\x00-\x1f\x7f-\xff\"\'\<\>]+)', p_unq)
        if m_remote:
            return m_remote.group(1)
    return p_unq.strip()


def resolve_conversation_workspace(cid, existing_inner_blobs, known_ws_uris, known_paths, brain_dir, conv_dir):
    """
    Mendeteksi workspace / project path dari berbagai sumber secara bertingkat:
    1. Inner blob di state.vscdb
    2. Heuristik brain .md via rc.infer_workspace_from_brain
    3. Brain log files (.system_generated/logs: overview.txt, transcript.jsonl)
    4. Brain user files (.md, .txt, .json)
    5. Langkah conversation database (.db)
    """
    # 1. Dari blob state.vscdb
    blob = existing_inner_blobs.get(cid)
    if blob:
        hint = rc.extract_workspace_hint(blob)
        if hint:
            cw = clean_workspace_path(hint)
            if cw:
                return cw

    # 2. Heuristik infer dari brain via rebuild_conversations
    inferred = rc.infer_workspace_from_brain(cid, known_ws_uris)
    if inferred:
        cw = clean_workspace_path(inferred)
        if cw:
            return cw

    # 3. Cek log brain (overview.txt / transcript.jsonl)
    b_path = os.path.join(brain_dir, cid, '.system_generated', 'logs')
    for log_name in ['overview.txt', 'transcript.jsonl']:
        fpath = os.path.join(b_path, log_name)
        if os.path.exists(fpath):
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    for idx, line in enumerate(f):
                        if idx > 150:
                            break
                        # Pola [URI] -> [CorpusName]
                        m = re.search(r'\[([^\]]+)\]\s*->\s*\[[^\]]+\]', line)
                        if m:
                            cand = clean_workspace_path(m.group(1).strip())
                            if cand and len(cand) > 3:
                                return cand
                        # Pola Active Document: d:\ADR\...
                        m_doc = re.search(r'Active Document:\s*([a-zA-Z]:\\[^\r\n\(]+)', line)
                        if m_doc:
                            doc_p = os.path.normpath(m_doc.group(1).strip()).replace('\\', '/')
                            for kp in known_paths:
                                if doc_p.lower().startswith(kp.lower()):
                                    return os.path.normpath(kp)
            except Exception:
                pass

    # 4. Cek file-file brain (notes, summaries, scratch)
    b_root = os.path.join(brain_dir, cid)
    if os.path.isdir(b_root):
        for root, dirs, files in os.walk(b_root):
            for fname in files:
                if fname.endswith(('.md', '.txt', '.json')):
                    try:
                        with open(os.path.join(root, fname), 'r', encoding='utf-8', errors='replace') as f:
                            text = f.read(32768)
                            for kp in known_paths:
                                if kp.lower() in text.lower().replace('\\', '/'):
                                    return os.path.normpath(kp)
                    except Exception:
                        pass

    # 5. Cek steps payload pada file .db
    if conv_dir:
        db_file = os.path.join(conv_dir, f"{cid}.db")
        if os.path.exists(db_file):
            try:
                conn = sqlite3.connect(db_file)
                cur = conn.cursor()
                cur.execute("SELECT step_payload FROM steps LIMIT 10")
                for r in cur.fetchall():
                    if r[0]:
                        text = str(r[0])
                        for kp in known_paths:
                            if kp.lower() in text.lower().replace('\\', '/'):
                                conn.close()
                                return os.path.normpath(kp)
                conn.close()
            except Exception:
                pass

    return None


def parse_selection(raw, display_list):
    """
    Mem-parsing input pengguna menjadi daftar item yang dipilih dari display_list.
    Mendukung:
    - Single index: '79'
    - Multiple index: '78, 79, 80' atau '78 79 80'
    - Rentang index: '5-10' atau '5 - 10'
    - Full/partial UUID: 'b57eb8ee' atau UUID lengkap
    - Keyword 'all' atau 'semua' untuk memilih seluruh percakapan
    - Kombinasi: '1, 3, 5-8, b57eb8ee'

    Mengembalikan tuple: (items, error_message)
    """
    raw = raw.strip()
    if not raw:
        return None, "Input tidak boleh kosong."

    if raw.lower() in ('all', 'semua'):
        return list(display_list), None

    # Normalisasi spasi di sekitar tanda minus untuk rentang angka (misal '1 - 5' -> '1-5')
    normalized = re.sub(r'(?<![a-zA-Z0-9])(\d+)\s*-\s*(\d+)(?![a-zA-Z0-9])', r'\1-\2', raw)
    normalized = normalized.replace(',', ' ').replace(';', ' ')
    tokens = normalized.split()

    if not tokens:
        return None, "Input tidak boleh kosong."

    selected = []
    seen_cids = set()

    for token in tokens:
        # Cek apakah token berupa rentang angka, misal "1-5"
        range_match = re.fullmatch(r'(\d+)-(\d+)', token)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start > end:
                return None, f"Rentang '{token}' tidak valid (angka awal lebih besar dari angka akhir)."
            if start < 1 or end > len(display_list):
                return None, f"Rentang '{token}' di luar jangkauan nomor yang tersedia (1 - {len(display_list)})."
            for idx in range(start, end + 1):
                item = display_list[idx - 1]
                if item[0] not in seen_cids:
                    seen_cids.add(item[0])
                    selected.append(item)
            continue

        # Cek apakah token berupa nomor urut tunggal
        if token.isdigit():
            idx = int(token)
            if idx < 1 or idx > len(display_list):
                return None, f"Nomor {idx} di luar jangkauan (1 - {len(display_list)})."
            item = display_list[idx - 1]
            if item[0] not in seen_cids:
                seen_cids.add(item[0])
                selected.append(item)
            continue

        # Token berupa UUID atau awalan UUID
        t_lower = token.lower()
        exact = [it for it in display_list if it[0].lower() == t_lower]
        if exact:
            match_item = exact[0]
        else:
            prefix_matches = [it for it in display_list if it[0].lower().startswith(t_lower)]
            if len(prefix_matches) == 1:
                match_item = prefix_matches[0]
            elif len(prefix_matches) > 1:
                return None, f"ID '{token}' ambigu ({len(prefix_matches)} percakapan cocok). Masukkan nomor urut atau ID lebih lengkap."
            else:
                return None, f"Tidak ditemukan percakapan dengan nomor atau ID '{token}'."

        if match_item[0] not in seen_cids:
            seen_cids.add(match_item[0])
            selected.append(match_item)

    return selected, None


def inject_conversation_to_db(db_path, new_entry_bytes, chosen_cids, backup_suffix="restore"):
    """
    Menyisipkan entri percakapan baru di urutan teratas database index,
    sekaligus membuat backup bertanggal dan membersihkan duplikat percakapan lama.
    Mendukung single CID (str) atau multiple CIDs (list/set/tuple).
    """
    if isinstance(chosen_cids, str):
        chosen_cids = [chosen_cids]

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        "SELECT value FROM ItemTable "
        "WHERE key='antigravityUnifiedStateSync.trajectorySummaries'"
    )
    row = cur.fetchone()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"trajectorySummaries_backup_{backup_suffix}_{stamp}.txt"
    backup_path = os.path.join(rc._backup_dir(), backup_name)

    if row and row[0]:
        try:
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(row[0])
        except OSError as e:
            conn.close()
            raise RuntimeError(
                f"Tidak dapat menulis rollback backup ke {backup_path}: {e}\n"
                f"    Operasi dibatalkan demi keamanan database."
            )
        existing_bytes = base64.b64decode(row[0])
        # Bersihkan entri lama jika percakapan ini sudah pernah ada sebelumnya
        for cid in chosen_cids:
            existing_bytes = remove_entry_by_cid(existing_bytes, cid)
    else:
        existing_bytes = b""
        backup_path = None

    # Letakkan entri baru di posisi paling atas agar muncul terdepan di sidebar
    combined = new_entry_bytes + existing_bytes
    encoded = base64.b64encode(combined).decode('utf-8')

    if row:
        cur.execute(
            "UPDATE ItemTable SET value=? "
            "WHERE key='antigravityUnifiedStateSync.trajectorySummaries'",
            (encoded,)
        )
    else:
        cur.execute(
            "INSERT INTO ItemTable (key, value) "
            "VALUES ('antigravityUnifiedStateSync.trajectorySummaries', ?)",
            (encoded,)
        )

    conn.commit()
    conn.close()
    return backup_path


def main():
    print()
    print("=" * 66)
    print("   Antigravity - Restore Satu / Beberapa Conversation")
    print("   [Mode Khusus: Antigravity IDE Saja]")
    print("=" * 66)
    print()

    conv_dir, brain_dir, db_paths = get_ide_specific_paths()

    if not db_paths:
        print("  ERROR: Database Antigravity IDE tidak ditemukan di:")
        for candidate in rc._DB_CANDIDATES:
            print(f"    {candidate}")
        print()
        print("  Pastikan Antigravity IDE sudah pernah dibuka minimal sekali.")
        input("\n  Tekan Enter untuk keluar...")
        return 1

    print(f"  Folder Percakapan : {conv_dir}")
    print(f"  Target Database   :")
    for db in db_paths:
        app_name = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(db))))
        print(f"    - {app_name}: {db}")
    print()

    # -- Cek apakah Antigravity sedang berjalan ----------------------
    if rc.is_antigravity_running():
        print("  PERINGATAN: Antigravity masih berjalan!")
        print("  Perubahan tidak akan efektif atau bisa tertimpa jika Antigravity masih terbuka.")
        print("  Silakan tutup Antigravity terlebih dahulu (File > Exit atau Task Manager).")
        print()
        choice = input("  Ketik Y jika ingin tetap lanjut, atau Enter/Q untuk keluar: ").strip().lower()
        if choice not in ('y', 'yes'):
            return 1
        print()

    print("  Memindai percakapan di disk (antigravity-ide saja)...")
    all_convs = list_all_conversations(conv_dir)

    if not all_convs:
        print(f"  Tidak ada percakapan ditemukan di: {conv_dir}")
        input("\n  Tekan Enter untuk keluar...")
        return 0

    print("  Membaca metadata dari database Antigravity IDE...")
    indexed_ids = get_conversation_already_in_db(db_paths)
    existing_titles, existing_inner_blobs = rc.extract_existing_metadata_from_paths(db_paths)
    known_ws_uris = rc.load_known_workspace_uris()

    known_paths = []
    for u in known_ws_uris:
        p = rc._uri_to_local_path(u)
        if p:
            p = os.path.normpath(p).replace("\\", "/")
            known_paths.append(p)
    known_paths.sort(key=len, reverse=True)

    markers = {"brain": "+", "preserved": "~", "conversation": "=", "fallback": "?"}

    print()
    print(f"  Ditemukan {len(all_convs)} percakapan di antigravity-ide (terbaru dulu):")
    print()
    print(f"  {'No':>4}  {'Status':<8} {'Src':<3}  {'Tanggal & Waktu':<17}  {'Conversation ID':<36}  Judul")
    print("  " + "-" * 115)

    display_list = []
    for i, (cid, fpath, mtime) in enumerate(all_convs, 1):
        date_str = time.strftime("%d %b %Y %H:%M", time.localtime(mtime))
        status = "[OK]  " if cid in indexed_ids else "[HILANG]"
        title, source = rc.resolve_title(cid, existing_titles, fpath)
        marker = markers.get(source, "?")
        ws_path = resolve_conversation_workspace(
            cid, existing_inner_blobs, known_ws_uris, known_paths, brain_dir, conv_dir
        )

        max_title_len = 45
        short_title = title[:max_title_len] + "..." if len(title) > max_title_len else title
        print(f"  {i:>4}  {status} [{marker}]  {date_str}  {cid}  {short_title}")
        print(" " * 32 + f"Path:  {ws_path or '-'}\n")
        display_list.append((cid, fpath, mtime, title, source, ws_path))

    print("  " + "-" * 115)
    print("  Status: [OK] = sudah ada di sidebar  |  [HILANG] = belum masuk index")
    print("  Sumber: [+] brain  [~] preserved  [=] prompt/db  [?] fallback")
    print()

    while True:
        raw = input("  Masukkan nomor atau ID percakapan (bisa lebih dari satu, misal: 1, 2, 5-8, atau Q untuk keluar): ").strip()
        if raw.lower() in ('q', 'quit'):
            print("  Dibatalkan.")
            return 0
        chosen_items, err = parse_selection(raw, display_list)
        if err:
            print(f"  {err}")
            continue
        break

    if len(chosen_items) == 1:
        chosen_cid, chosen_path, chosen_mtime, chosen_title, chosen_source, chosen_ws = chosen_items[0]
        print()
        print("  Percakapan dipilih:")
        print(f"    ID           : {chosen_cid}")
        print(f"    Judul        : {chosen_title}")
        print(f"    Project Path : {chosen_ws or '-'}")
        print(f"    Sumber       : {chosen_source}")
        print(f"    File         : {chosen_path}")
        print()

        if chosen_cid in indexed_ids:
            print("  INFO: Percakapan ini sudah ada di sidebar.")
            cont = input("  Tetap ingin di-restore ulang (pindah ke urutan paling atas)? (y/N): ").strip().lower()
            if cont not in ('y', 'yes'):
                print("  Dibatalkan.")
                input("\n  Tekan Enter untuk keluar...")
                return 0
    else:
        print()
        print(f"  Percakapan dipilih ({len(chosen_items)} percakapan):")
        for idx, (cid, path, mtime, title, source, ws) in enumerate(chosen_items, 1):
            status_tag = "[OK]  " if cid in indexed_ids else "[HILANG]"
            short = title[:50] + "..." if len(title) > 50 else title
            print(f"    {idx:>2}. {status_tag} {cid} - {short}")
            print(" " * 8 + f"Path:  {ws or '-'}")
        print()

        already_indexed = [item for item in chosen_items if item[0] in indexed_ids]
        if already_indexed:
            print(f"  INFO: {len(already_indexed)} dari {len(chosen_items)} percakapan sudah ada di sidebar.")
            cont = input("  Tetap ingin me-restore ulang (pindah ke urutan paling atas)? (y/N): ").strip().lower()
            if cont not in ('y', 'yes'):
                print("  Dibatalkan.")
                input("\n  Tekan Enter untuk keluar...")
                return 0

    # Urutkan berdasarkan waktu modifikasi terbaru lebih dulu (mtime descending)
    # sehingga saat ditaruh di urutan teratas sidebar, urutannya tetap kronologis
    sorted_items = sorted(chosen_items, key=lambda x: x[2], reverse=True)

    all_new_entries_bytes = b""
    chosen_cids = [item[0] for item in sorted_items]

    for cid, fpath, mtime, title, source, ws_path in sorted_items:
        inner_data = existing_inner_blobs.get(cid)
        if ws_path and len(sorted_items) == 1:
            display_ws = os.path.basename(ws_path) if not rc._is_remote_uri(ws_path) else ws_path
            print(f"  Workspace terdeteksi: {display_ws} ({ws_path})")

        source_is_db = bool(fpath and fpath.lower().endswith(".db"))

        entry = rc.build_trajectory_entry(
            cid,
            title,
            existing_inner_data=inner_data,
            workspace_path=ws_path,
            conversation_mtime=mtime,
            source_is_db=source_is_db
        )
        all_new_entries_bytes += rc.encode_length_delimited(1, entry)

    print()
    print("  Menyimpan ke database Antigravity IDE...")
    for db_path in db_paths:
        app_name = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(db_path))))
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", app_name).strip("_").lower() + "_restore"
        backup_path = inject_conversation_to_db(db_path, all_new_entries_bytes, chosen_cids, suffix)
        print(f"    {app_name}: berhasil diperbarui")
        if backup_path:
            print(f"      backup: {backup_path}")

    print()
    print("  " + "=" * 58)
    if len(sorted_items) == 1:
        print("  BERHASIL! Percakapan telah di-restore ke urutan teratas.")
        print(f"  Judul: \"{sorted_items[0][3]}\"")
    else:
        print(f"  BERHASIL! {len(sorted_items)} percakapan telah di-restore ke urutan teratas.")
    print("  " + "=" * 58)
    print()
    print("  LANGKAH SELANJUTNYA:")
    print("    1. Pastikan Antigravity sudah tertutup penuh")
    print("    2. Buka Antigravity — percakapan seharusnya sudah muncul di sidebar")
    print("    3. Jika belum muncul, restart PC Anda lalu buka kembali Antigravity")
    print()
    input("  Tekan Enter untuk keluar...")
    return 0


if __name__ == "__main__":
    sys.exit(main())

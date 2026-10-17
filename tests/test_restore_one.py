import unittest
import base64
import os
import tempfile
import sqlite3
import restore_one
import rebuild_conversations as rc

class RestoreOneTests(unittest.TestCase):
    def test_remove_entry_by_cid(self):
        # Create two protobuf entries
        entry1_inner = rc.encode_string_field(1, "Title 1")
        entry1_sub = rc.encode_string_field(1, base64.b64encode(entry1_inner).decode('utf-8'))
        entry1 = rc.encode_string_field(1, "cid-1") + rc.encode_length_delimited(2, entry1_sub)
        entry1_bytes = rc.encode_length_delimited(1, entry1)

        entry2_inner = rc.encode_string_field(1, "Title 2")
        entry2_sub = rc.encode_string_field(1, base64.b64encode(entry2_inner).decode('utf-8'))
        entry2 = rc.encode_string_field(1, "cid-2") + rc.encode_length_delimited(2, entry2_sub)
        entry2_bytes = rc.encode_length_delimited(1, entry2)

        combined = entry1_bytes + entry2_bytes

        # Remove cid-1
        cleaned = restore_one.remove_entry_by_cid(combined, "cid-1")
        self.assertEqual(cleaned, entry2_bytes)

        # Remove cid-2
        cleaned2 = restore_one.remove_entry_by_cid(combined, "cid-2")
        self.assertEqual(cleaned2, entry1_bytes)

        # Remove non-existent cid
        cleaned3 = restore_one.remove_entry_by_cid(combined, "cid-999")
        self.assertEqual(cleaned3, combined)

    def test_inject_conversation_to_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "state.vscdb")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            conn.close()

            entry_inner = rc.encode_string_field(1, "Test Title")
            entry_sub = rc.encode_string_field(1, base64.b64encode(entry_inner).decode('utf-8'))
            entry = rc.encode_string_field(1, "test-cid") + rc.encode_length_delimited(2, entry_sub)
            new_bytes = rc.encode_length_delimited(1, entry)

            backup = restore_one.inject_conversation_to_db(db_path, new_bytes, "test-cid", "test")
            self.assertIsNone(backup)  # Initial empty table has no previous backup

            # Re-inject (should create backup and not duplicate)
            backup2 = restore_one.inject_conversation_to_db(db_path, new_bytes, "test-cid", "test")
            self.assertIsNotNone(backup2)
            self.assertTrue(os.path.exists(backup2))

            # Verify only 1 entry exists in database
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.trajectorySummaries'")
            row = cur.fetchone()
            conn.close()

            decoded = base64.b64decode(row[0])
            count = 0
            pos = 0
            while pos < len(decoded):
                tag, pos = rc.decode_varint(decoded, pos)
                l, pos = rc.decode_varint(decoded, pos)
                pos += l
                count += 1
            self.assertEqual(count, 1)

            # Cleanup backup file
            if backup2 and os.path.exists(backup2):
                os.remove(backup2)

    def test_parse_selection(self):
        dummy_list = [
            ("cid-1111-aaaa", "/path/1.db", 1000, "Title 1", "conversation"),
            ("cid-2222-bbbb", "/path/2.db", 2000, "Title 2", "conversation"),
            ("cid-3333-cccc", "/path/3.db", 3000, "Title 3", "brain"),
            ("cid-4444-dddd", "/path/4.db", 4000, "Title 4", "prompt"),
            ("cid-5555-eeee", "/path/5.db", 5000, "Title 5", "fallback"),
        ]

        # Single number
        items, err = restore_one.parse_selection("1", dummy_list)
        self.assertIsNone(err)
        self.assertEqual([x[0] for x in items], ["cid-1111-aaaa"])

        # Multiple numbers comma / space
        items, err = restore_one.parse_selection("1, 3, 5", dummy_list)
        self.assertIsNone(err)
        self.assertEqual([x[0] for x in items], ["cid-1111-aaaa", "cid-3333-cccc", "cid-5555-eeee"])

        # Range
        items, err = restore_one.parse_selection("2-4", dummy_list)
        self.assertIsNone(err)
        self.assertEqual([x[0] for x in items], ["cid-2222-bbbb", "cid-3333-cccc", "cid-4444-dddd"])

        # Range with space & extra items
        items, err = restore_one.parse_selection("1 - 2, 5", dummy_list)
        self.assertIsNone(err)
        self.assertEqual([x[0] for x in items], ["cid-1111-aaaa", "cid-2222-bbbb", "cid-5555-eeee"])

        # UUID prefix & full UUID
        items, err = restore_one.parse_selection("cid-2222, cid-3333-cccc", dummy_list)
        self.assertIsNone(err)
        self.assertEqual([x[0] for x in items], ["cid-2222-bbbb", "cid-3333-cccc"])

        # Keyword 'all' or 'semua'
        items, err = restore_one.parse_selection("all", dummy_list)
        self.assertIsNone(err)
        self.assertEqual(len(items), 5)

        # Deduplication
        items, err = restore_one.parse_selection("1, 1, 2, cid-1111", dummy_list)
        self.assertIsNone(err)
        self.assertEqual([x[0] for x in items], ["cid-1111-aaaa", "cid-2222-bbbb"])

        # Errors: empty, out of bounds, invalid range, not found
        _, err = restore_one.parse_selection("", dummy_list)
        self.assertIsNotNone(err)

        _, err = restore_one.parse_selection("99", dummy_list)
        self.assertIn("di luar jangkauan", err)

        _, err = restore_one.parse_selection("5-2", dummy_list)
        self.assertIn("tidak valid", err)

        _, err = restore_one.parse_selection("cid-nonexistent", dummy_list)
        self.assertIn("Tidak ditemukan", err)

    def test_inject_multiple_conversations_to_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "state.vscdb")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            conn.close()

            entry1_inner = rc.encode_string_field(1, "Title 1")
            entry1_sub = rc.encode_string_field(1, base64.b64encode(entry1_inner).decode('utf-8'))
            entry1 = rc.encode_string_field(1, "cid-multi-1") + rc.encode_length_delimited(2, entry1_sub)
            entry1_bytes = rc.encode_length_delimited(1, entry1)

            entry2_inner = rc.encode_string_field(1, "Title 2")
            entry2_sub = rc.encode_string_field(1, base64.b64encode(entry2_inner).decode('utf-8'))
            entry2 = rc.encode_string_field(1, "cid-multi-2") + rc.encode_length_delimited(2, entry2_sub)
            entry2_bytes = rc.encode_length_delimited(1, entry2)

            combined_new = entry1_bytes + entry2_bytes
            cids = ["cid-multi-1", "cid-multi-2"]

            backup = restore_one.inject_conversation_to_db(db_path, combined_new, cids, "test_multi")
            self.assertIsNone(backup)

            # Check both entries exist in database
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.trajectorySummaries'")
            row = cur.fetchone()
            conn.close()

            decoded = base64.b64decode(row[0])
            count = 0
            pos = 0
            while pos < len(decoded):
                tag, pos = rc.decode_varint(decoded, pos)
                l, pos = rc.decode_varint(decoded, pos)
                pos += l
                count += 1
            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()

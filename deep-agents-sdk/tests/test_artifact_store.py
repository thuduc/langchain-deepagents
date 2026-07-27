"""Tests for artifact storage.

The local store is exercised for real. The S3 store runs against a stub client,
which verifies key construction, streaming, and prefix deletion without AWS.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from deep_agents_app.services.artifact_store import (  # noqa: E402
    ArtifactStoreError,
    LocalArtifactStore,
    S3ArtifactStore,
    create_store,
    validate_key,
)


KEY = "u-alice/p-hpi/s-1/r-42/chart.png"


class KeyValidationTests(unittest.TestCase):
    def test_accepts_a_normal_key(self):
        self.assertEqual(validate_key(KEY), KEY)

    def test_strips_surrounding_slashes(self):
        self.assertEqual(validate_key(f"/{KEY}/"), KEY)

    def test_rejects_traversal_and_absolute_paths(self):
        for bad in ("../secrets", "u/../../etc/passwd", "..", "", "   ", "a\\b"):
            with self.subTest(key=bad), self.assertRaises(ArtifactStoreError):
                validate_key(bad)

    def test_harmless_dot_segments_normalise_away(self):
        # '.' collapses and cannot escape; only '..' is a traversal risk.
        self.assertEqual(validate_key("a/./b"), "a/b")


class LocalArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="artifact-store-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.root = self.tmp_dir / "artifacts"
        self.store = LocalArtifactStore(self.root)
        self.store.preflight()

    def _source(self, name: str = "chart.png", body: bytes = b"png-bytes") -> Path:
        path = self.tmp_dir / "staging" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_put_then_read_back(self):
        self.store.put(KEY, self._source())
        self.assertEqual(b"".join(self.store.open_stream(KEY)), b"png-bytes")

    def test_put_is_a_no_op_when_already_staged_in_place(self):
        target = self.root / KEY
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"already-here")
        self.store.put(KEY, target)
        self.assertEqual(target.read_bytes(), b"already-here")

    def test_large_files_stream_in_chunks(self):
        body = b"x" * (700 * 1024)
        self.store.put(KEY, self._source(body=body))
        chunks = list(self.store.open_stream(KEY))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(b"".join(chunks), body)

    def test_missing_object_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            list(self.store.open_stream(KEY))

    def test_missing_object_raises_before_any_bytes_are_yielded(self):
        """The download route can only answer 404 while it still owns the response.

        A generator function would defer this until the first read, by which
        point the 200 and its headers have already gone out and the client gets
        a truncated body instead of an error.
        """
        with self.assertRaises(FileNotFoundError):
            self.store.open_stream(KEY)  # no iteration

    def test_an_unsafe_key_is_refused_before_any_bytes_are_yielded(self):
        with self.assertRaises(ArtifactStoreError):
            self.store.open_stream("../../etc/passwd")

    def test_delete_prefix_removes_a_whole_session(self):
        self.store.put(KEY, self._source())
        self.store.put("u-alice/p-hpi/s-1/r-99/other.csv", self._source("other.csv"))
        self.store.put("u-alice/p-hpi/s-2/r-7/keep.csv", self._source("keep.csv"))

        self.store.delete_prefix("u-alice/p-hpi/s-1")

        with self.assertRaises(FileNotFoundError):
            list(self.store.open_stream(KEY))
        with self.assertRaises(FileNotFoundError):
            list(self.store.open_stream("u-alice/p-hpi/s-1/r-99/other.csv"))
        self.assertEqual(
            b"".join(self.store.open_stream("u-alice/p-hpi/s-2/r-7/keep.csv")),
            b"png-bytes",
        )

    def test_traversal_key_cannot_escape_the_root(self):
        with self.assertRaises(ArtifactStoreError):
            self.store.put("../escaped.txt", self._source())


class StubS3Client:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        return {}

    def upload_file(self, filename, bucket, key):
        self.calls.append(("upload_file", key))
        self.objects[key] = Path(filename).read_bytes()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        body = self.objects[Key]

        class _Body:
            def iter_chunks(self, size):
                yield body

        return {"Body": _Body()}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_paginator(self, name):
        objects = self.objects

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                yield {
                    "Contents": [
                        {"Key": key} for key in objects if key.startswith(Prefix)
                    ]
                }

        return _Paginator()

    def delete_objects(self, Bucket, Delete):
        for entry in Delete["Objects"]:
            self.objects.pop(entry["Key"], None)


class StubBotoSession:
    def __init__(self, client):
        self._client = client

    def client(self, service, **kwargs):
        return self._client


class S3ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="artifact-store-s3-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.s3 = StubS3Client()
        self.store = S3ArtifactStore(
            bucket="test-bucket",
            prefix="artifacts",
            region="us-east-1",
            boto_session=StubBotoSession(self.s3),
        )

    def _source(self, name="chart.png", body=b"png-bytes") -> Path:
        path = self.tmp_dir / name
        path.write_bytes(body)
        return path

    def test_object_key_is_prefixed(self):
        self.store.put(KEY, self._source())
        self.assertIn(f"artifacts/{KEY}", self.s3.objects)

    def test_put_removes_the_local_staging_copy(self):
        source = self._source()
        self.store.put(KEY, source)
        self.assertFalse(source.exists())

    def test_read_back_streams_the_bytes(self):
        self.store.put(KEY, self._source())
        self.assertEqual(b"".join(self.store.open_stream(KEY)), b"png-bytes")

    def test_missing_object_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.store.open_stream(KEY)

    def test_delete_prefix_scopes_to_the_session(self):
        self.store.put(KEY, self._source())
        self.store.put("u-alice/p-hpi/s-2/r-7/keep.csv", self._source("keep.csv"))

        self.store.delete_prefix("u-alice/p-hpi/s-1")

        self.assertNotIn(f"artifacts/{KEY}", self.s3.objects)
        self.assertIn("artifacts/u-alice/p-hpi/s-2/r-7/keep.csv", self.s3.objects)

    def test_preflight_requires_a_bucket(self):
        store = S3ArtifactStore(bucket="", boto_session=StubBotoSession(self.s3))
        store.bucket = ""
        with self.assertRaisesRegex(ArtifactStoreError, "ARTIFACT_S3_BUCKET"):
            store.preflight()


class StoreSelectionTests(unittest.TestCase):
    def test_selects_by_name(self):
        self.assertEqual(create_store("local", root=Path("/tmp")).name, "local")
        self.assertEqual(create_store("s3").name, "s3")

    def test_unknown_store_is_rejected(self):
        with self.assertRaisesRegex(ArtifactStoreError, "Unknown artifact store"):
            create_store("does-not-exist")


if __name__ == "__main__":
    unittest.main()

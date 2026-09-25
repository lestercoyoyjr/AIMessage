"""Google Drive cold-tier backend (v0.3.0) — encrypted artifact overflow to Drive.

Design keeps the Google wiring OUT of the testable path: `GDriveColdStore` speaks to a small
`DriveClient` interface (upload / download / exists / free_bytes), so all the cold-store logic is
unit-tested against a fake. The REAL client (`GoogleDriveClient`) wraps google-api-python-client and
is built via `google_drive_client(...)` — an OPTIONAL dependency (`pip install ".[gdrive]"`), lazily
imported, and NOT unit-tested (it needs real OAuth credentials, like the live Centralaizer path).

Everything stored is already ciphertext (the TieredArtifactCache encrypts before spilling), so Drive
only ever holds opaque blobs — zero cleartext egress. When Drive is full, the tiered cache surfaces a
`storage.full` event; the app shows the Google One upgrade link. It NEVER buys storage — that's the
user's action.
"""
from __future__ import annotations

import re

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
GOOGLE_ONE_UPGRADE_URL = "https://one.google.com/storage"   # link only — the app never auto-purchases


class GDriveColdStore:
    """A ColdStore backed by a DriveClient. Keys are content hashes (sha256 hex); files are named
    `{hash}.enc`. Interchangeable with LocalDirColdStore in TieredArtifactCache."""

    def __init__(self, client):
        self._c = client

    def _name(self, key: str) -> str:
        if not _HEX64.match(key):
            raise ValueError(f"cold-store key must be a sha256 hex digest, got {key!r}")
        return f"{key}.enc"

    def put(self, key: str, data: bytes) -> None:
        self._c.upload(self._name(key), data)

    def get(self, key: str) -> bytes | None:
        if not _HEX64.match(key):
            return None
        return self._c.download(self._name(key))

    def __contains__(self, key: str) -> bool:
        return _HEX64.match(key) is not None and self._c.exists(self._name(key))

    def free_bytes(self):
        """Remaining Drive space in bytes, or None if unknown/unlimited (drives the both-full check)."""
        return self._c.free_bytes()


class GoogleDriveClient:
    """Real DriveClient over Drive v3. Requires the `[gdrive]` extra + an authenticated service.
    Not unit-tested (needs live OAuth); build it with `google_drive_client(...)`."""

    def __init__(self, service, folder_id: str):
        self._svc = service
        self._folder = folder_id

    def _find_id(self, name: str):
        q = (f"name = '{name}' and '{self._folder}' in parents and trashed = false")
        res = self._svc.files().list(q=q, spaces="drive", fields="files(id)",
                                     pageSize=1).execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def upload(self, name: str, data: bytes) -> None:
        from googleapiclient.http import MediaInMemoryUpload  # lazy: optional dep
        media = MediaInMemoryUpload(data, mimetype="application/octet-stream", resumable=False)
        fid = self._find_id(name)
        if fid:
            self._svc.files().update(fileId=fid, media_body=media).execute()
        else:
            self._svc.files().create(body={"name": name, "parents": [self._folder]},
                                     media_body=media, fields="id").execute()

    def download(self, name: str) -> bytes | None:
        import io

        from googleapiclient.http import MediaIoBaseDownload  # lazy
        fid = self._find_id(name)
        if not fid:
            return None
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self._svc.files().get_media(fileId=fid))
        done = False
        while not done:
            _status, done = dl.next_chunk()
        return buf.getvalue()

    def exists(self, name: str) -> bool:
        return self._find_id(name) is not None

    def free_bytes(self):
        quota = self._svc.about().get(fields="storageQuota").execute().get("storageQuota", {})
        limit = quota.get("limit")
        if limit is None:
            return None                              # unlimited / not reported
        return max(int(limit) - int(quota.get("usage", 0)), 0)


def google_drive_client(credentials, folder_name: str = "AIMessage-cold") -> GoogleDriveClient:
    """Build a GoogleDriveClient from OAuth `credentials` (google.oauth2.credentials.Credentials),
    creating the app folder if needed. Requires `pip install ".[gdrive]"`. The caller performs the
    OAuth consent — this code never creates accounts or grants scopes on the user's behalf."""
    from googleapiclient.discovery import build  # lazy: optional dep
    svc = build("drive", "v3", credentials=credentials, cache_discovery=False)
    q = (f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' "
         f"and trashed = false")
    found = svc.files().list(q=q, spaces="drive", fields="files(id)", pageSize=1).execute().get("files", [])
    if found:
        folder_id = found[0]["id"]
    else:
        folder_id = svc.files().create(
            body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
            fields="id").execute()["id"]
    return GoogleDriveClient(svc, folder_id)

import json
import base64
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

class TransmissionError(Exception):
    pass

class TransmissionClient:
    def __init__(self, rpc_url: str = "http://127.0.0.1:9091/transmission/rpc", timeout: float = 30.0, username: str = "", password: Optional[str] = None):
        self.rpc_url = rpc_url
        self.timeout = timeout
        self.username = username
        self.password = password
        self.session_id: Optional[str] = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        if self.username and self.password:
            auth_str = f"{self.username}:{self.password}".encode("utf-8")
            headers["Authorization"] = f"Basic {base64.b64encode(auth_str).decode('ascii')}"

        payload = json.dumps({"method": method, "arguments": arguments or {}}).encode("utf-8")
        req = urllib.request.Request(self.rpc_url, data=payload, headers=headers)

        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 409:
                self.session_id = e.headers.get("X-Transmission-Session-Id")
                headers["X-Transmission-Session-Id"] = self.session_id
                req = urllib.request.Request(self.rpc_url, data=payload, headers=headers)
                try:
                    with self.opener.open(req, timeout=self.timeout) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except Exception as retry_error:
                    raise TransmissionError(
                        f"Impossible de contacter Transmission: {retry_error}"
                    ) from retry_error
            raise TransmissionError(f"Erreur HTTP {e.code}: {e.reason}") from e
        except Exception as e:
            raise TransmissionError(f"Impossible de contacter Transmission: {e}") from e

    def test_connection(self) -> bool:
        try:
            res = self.request("session-get")
            return res.get("result") == "success"
        except Exception:
            return False

    def get_torrents(self, fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        default_fields = [
            "id", "name", "status", "percentDone", "rateDownload", "rateUpload",
            "uploadRatio", "downloadDir", "files", "fileStats"
        ]
        args = {"fields": fields or default_fields}
        res = self.request("torrent-get", args)
        return res.get("arguments", {}).get("torrents", [])

    def set_file_priorities(
        self,
        torrent_id: int,
        wanted_indices: Optional[List[int]] = None,
        unwanted_indices: Optional[List[int]] = None,
        chunk_size: int = 1000,
    ) -> None:
        """Applique les priorités de fichiers par lots (gros packs = milliers d'index)."""
        wanted = list(wanted_indices or [])
        unwanted = list(unwanted_indices or [])
        total = max(len(wanted), len(unwanted))
        for start in range(0, total, chunk_size):
            args: Dict[str, Any] = {"ids": [torrent_id]}
            if wanted:
                args["files-wanted"] = wanted[start:start + chunk_size]
            if unwanted:
                args["files-unwanted"] = unwanted[start:start + chunk_size]
            self.request("torrent-set", args)

    def get_torrent_file_count(self, torrent_id: int) -> int:
        """Retourne le nombre de fichiers d'un torrent (pour tout désélectionner)."""
        data = self.request(
            "torrent-get", {"ids": [torrent_id], "fields": ["id", "files"]}
        ).get("arguments", {}).get("torrents", [])
        if not data:
            raise TransmissionError(f"Torrent {torrent_id} introuvable")
        return len(data[0].get("files", []))

    def select_nothing(self, torrent_id: int) -> int:
        """Désélectionne tous les fichiers d'un torrent. Retourne le nombre de fichiers."""
        count = self.get_torrent_file_count(torrent_id)
        if count:
            self.set_file_priorities(torrent_id, unwanted_indices=list(range(count)))
        return count

    def start_torrent(self, torrent_id: int) -> None:
        """Démarre ou reprend un torrent dans Transmission."""
        self.request("torrent-start", {"ids": [torrent_id]})

    def stop_torrent(self, torrent_id: int) -> None:
        """Met en pause un torrent dans Transmission."""
        self.request("torrent-stop", {"ids": [torrent_id]})

    def verify_torrent(self, torrent_id: int) -> None:
        """Vérifie l'intégrité des données d'un torrent (verify-data)."""
        self.request("torrent-verify", {"ids": [torrent_id]})

    def add_torrent(self, torrent_file: str, pause: bool = True) -> int:
        """Ajoute un fichier .torrent à Transmission.

        Returns the torrent ID.
        """
        args: Dict[str, Any] = {"filename": torrent_file, "pause": str(pause).lower()}
        res = self.request("torrent-add", args)
        torrent_id = res.get("torrent-add")
        if torrent_id is None:
            raise TransmissionError("Impossible d'ajouter le torrent")
        return torrent_id

    def get_session_stats(self) -> Dict[str, Any]:
        res = self.request("session-stats")
        return res.get("arguments", {})

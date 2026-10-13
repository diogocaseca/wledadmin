"""Releases de firmware no GitHub e cache local dos binários.

- A lista de releases fica em cache (memória + arquivo), porque a API sem token permite
  só 60 requisições por hora.
- Os binários são guardados em DATA_DIR/firmware/<tag>/ e conferidos pelo SHA-256 que
  o GitHub publica para cada arquivo (campo "digest" do asset).
- Os builds "nightly" nunca aparecem; betas só se a opção estiver ligada.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from ..wled.firmware import MAX_IMAGE_BYTES, parse_version

log = logging.getLogger(__name__)

RELEASES_TTL = 3600.0
KEEP_TAG_DIRS = 6
KEEP_CUSTOM_FILES = 10


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int
    url: str
    sha256: str | None


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    prerelease: bool
    published_at: str
    html_url: str
    assets: tuple[ReleaseAsset, ...]

    @property
    def version(self) -> tuple[int, int, int] | None:
        return parse_version(self.tag)

    @property
    def version_str(self) -> str:
        return self.tag.removeprefix("v")

    def asset(self, name: str) -> ReleaseAsset | None:
        return next((a for a in self.assets if a.name == name), None)

    def find_asset(self, release_name: str) -> ReleaseAsset | None:
        """Arquivo exato para um release (ex.: "ESP32" → WLED_16.0.1_ESP32.bin).

        Aceita o nome com e sem "_V4" (o WLED trata os dois como equivalentes) e prefere
        .bin.gz para ESP8266 de 2 MB (ESP02), que não comportam o .bin descompactado.
        """
        names = [release_name]
        if release_name.endswith("_V4"):
            names.append(release_name[:-3])
        for rel in names:
            exts = (".bin.gz", ".bin") if rel.upper().startswith("ESP02") else (".bin",)
            for ext in exts:
                if found := self.asset(f"WLED_{self.version_str}_{rel}{ext}"):
                    return found
        return None


def _is_nightly(raw: dict) -> bool:
    return "nightly" in (raw.get("tag_name", "") + " " + (raw.get("name") or "")).lower()


def _parse_release(raw: dict) -> Release:
    assets = []
    for a in raw.get("assets", []):
        name = a.get("name", "")
        if not re.fullmatch(r"WLED_.+\.bin(\.gz)?", name):
            continue
        digest = a.get("digest") or ""
        assets.append(ReleaseAsset(
            name=name,
            size=int(a.get("size") or 0),
            url=a["browser_download_url"],
            sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else None,
        ))
    return Release(
        tag=raw["tag_name"],
        name=raw.get("name") or raw["tag_name"],
        prerelease=bool(raw.get("prerelease")),
        published_at=raw.get("published_at") or "",
        html_url=raw.get("html_url") or "",
        assets=tuple(sorted(assets, key=lambda a: a.name)),
    )


class ReleaseService:
    def __init__(
        self,
        data_dir: Path,
        *,
        api_url: str,
        token: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._dir = data_dir / "firmware"
        self._dir.mkdir(parents=True, exist_ok=True)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "wledadmin"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=api_url,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )
        self._cache: dict[str, tuple[float, list[Release]]] = {}
        self._download_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # ---- lista de releases -------------------------------------------------------------

    def _cache_file(self, repo: str) -> Path:
        return self._dir / f"releases-{repo.replace('/', '__')}.json"

    async def list_releases(
        self, repo: str, *, include_prereleases: bool, force: bool = False
    ) -> list[Release]:
        releases = await self._all_releases(repo, force)
        return [r for r in releases if include_prereleases or not r.prerelease]

    async def get_release(self, repo: str, tag: str) -> Release:
        for r in await self._all_releases(repo, False):
            if r.tag == tag:
                return r
        raise ReleaseError(f"versão {tag} não encontrada em {repo}")

    async def _all_releases(self, repo: str, force: bool) -> list[Release]:
        cached = self._cache.get(repo)
        if cached and not force and time.monotonic() - cached[0] < RELEASES_TTL:
            return cached[1]
        try:
            resp = await self._http.get(f"/repos/{repo}/releases", params={"per_page": 50})
        except httpx.HTTPError as e:
            return self._fallback(repo, f"sem acesso ao GitHub ({type(e).__name__})")
        if resp.status_code == 404:
            raise ReleaseError(f"repositório {repo} não encontrado no GitHub")
        if resp.status_code in (403, 429):
            return self._fallback(repo, "limite de requisições da API do GitHub atingido "
                                        "(defina WLEDADMIN_GITHUB_TOKEN para aumentar)")
        if resp.status_code >= 400:
            return self._fallback(repo, f"GitHub respondeu HTTP {resp.status_code}")
        raw = resp.json()
        releases = [
            _parse_release(r) for r in raw
            if not r.get("draft") and not _is_nightly(r) and parse_version(r.get("tag_name"))
        ]
        releases.sort(key=lambda r: (r.version, not r.prerelease), reverse=True)
        self._cache[repo] = (time.monotonic(), releases)
        try:
            self._cache_file(repo).write_text(json.dumps([asdict(r) for r in releases]))
        except OSError:
            log.warning("Não consegui gravar o cache de releases")
        return releases

    def _fallback(self, repo: str, reason: str) -> list[Release]:
        """Sem GitHub: usa a última lista salva em disco, se houver."""
        if cached := self._cache.get(repo):
            return cached[1]
        try:
            raw = json.loads(self._cache_file(repo).read_text())
        except (OSError, ValueError):
            raise ReleaseError(reason) from None
        releases = [
            Release(**{**r, "assets": tuple(ReleaseAsset(**a) for a in r["assets"])})
            for r in raw
        ]
        log.warning("%s; usando a lista de releases salva", reason)
        return releases

    # ---- binários ----------------------------------------------------------------------

    async def fetch_asset(self, release: Release, asset: ReleaseAsset) -> bytes:
        """Bytes do binário, do cache ou do GitHub, sempre conferidos pelo SHA-256."""
        path = self._dir / release.tag / asset.name
        async with self._download_lock:
            if path.exists():
                data = path.read_bytes()
                if self._matches(data, asset):
                    return data
                path.unlink()
            data = await self._download(asset)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            self._prune_tags()
            return data

    def _matches(self, data: bytes, asset: ReleaseAsset) -> bool:
        if asset.sha256:
            return hashlib.sha256(data).hexdigest() == asset.sha256
        return len(data) == asset.size

    async def _download(self, asset: ReleaseAsset) -> bytes:
        if asset.size > MAX_IMAGE_BYTES:
            raise ReleaseError(f"{asset.name}: arquivo grande demais")
        try:
            resp = await self._http.get(asset.url)
        except httpx.HTTPError as e:
            raise ReleaseError(f"falha ao baixar {asset.name} ({type(e).__name__})") from e
        if resp.status_code != 200:
            raise ReleaseError(f"falha ao baixar {asset.name} (HTTP {resp.status_code})")
        data = resp.content
        if not self._matches(data, asset):
            raise ReleaseError(f"{asset.name}: o arquivo baixado não confere com o SHA-256 "
                               "publicado pelo GitHub")
        return data

    def _prune_tags(self) -> None:
        dirs = sorted(
            (p for p in self._dir.iterdir() if p.is_dir() and p.name != "custom"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in dirs[KEEP_TAG_DIRS:]:
            shutil.rmtree(old, ignore_errors=True)

    # ---- .bin próprio ------------------------------------------------------------------

    def store_custom(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        folder = self._dir / "custom"
        folder.mkdir(exist_ok=True)
        (folder / f"{digest}.bin").write_bytes(data)
        files = sorted(folder.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP_CUSTOM_FILES:]:
            old.unlink(missing_ok=True)
        return digest

    def load_custom(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ReleaseError("identificador de arquivo inválido")
        path = self._dir / "custom" / f"{digest}.bin"
        try:
            data = path.read_bytes()
        except OSError:
            raise ReleaseError("arquivo enviado não encontrado; envie de novo") from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise ReleaseError("arquivo enviado corrompido; envie de novo")
        return data

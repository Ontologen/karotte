"""Pin an image tag to its digest, and read its platform and working directory,
from the registry over HTTP: no container engine involved, so an import reads
the same thing on every machine.

Anonymous pulls only, through the Docker registry token flow that Docker Hub,
GHCR, Quay and public ECR all speak. Requests go one at a time with backoff,
since anonymous pulls are rate limited (public ECR answers 429 quickly).
"""

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any

_MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
_INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
_DEFAULT_PLATFORM = ("linux", "amd64")
_ATTEMPTS = 8
_TIMEOUT_S = 60


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class ResolvedImage:
    ref: str
    """``repository:tag@sha256:…``, which pulls exactly this image."""
    platform: str
    workdir: str | None


@dataclass(frozen=True)
class _Ref:
    registry: str
    repository: str
    tag: str | None
    digest: str | None


def resolve(image: str) -> ResolvedImage:
    """The digest-pinned reference of ``image``, its platform and its working
    directory. For a multi-platform image, its linux/amd64 variant."""
    ref = _parse(image)
    session = _Session(ref)
    manifest, digest = session.manifest(ref.digest or ref.tag or "latest")
    if manifest.get("mediaType") in _INDEX_TYPES or "manifests" in manifest:
        entry = _pick_platform(manifest)
        manifest, digest = session.manifest(entry["digest"])
    config = session.blob(manifest["config"]["digest"])
    platform = f"{config.get('os', 'linux')}/{config.get('architecture', 'amd64')}"
    workdir = (config.get("config") or {}).get("WorkingDir") or None
    name = f"{ref.repository}:{ref.tag}" if ref.tag else ref.repository
    host = "" if ref.registry == "registry-1.docker.io" else f"{ref.registry}/"
    return ResolvedImage(
        ref=f"{host}{name}@{digest}", platform=platform, workdir=workdir
    )


def _parse(image: str) -> _Ref:
    name, _, digest = image.partition("@")
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "registry-1.docker.io", name
        if "/" not in path:
            path = f"library/{path}"
    repository, colon, tag = path.rpartition(":")
    if not colon or "/" in tag:
        repository, tag = path, ""
    return _Ref(registry, repository, tag or None, digest or None)


def _pick_platform(index: dict[str, Any]) -> dict[str, Any]:
    for entry in index.get("manifests", []):
        platform = entry.get("platform") or {}
        if (platform.get("os"), platform.get("architecture")) == _DEFAULT_PLATFORM:
            return entry
    raise RegistryError("the image index has no linux/amd64 variant")


class _Session:
    def __init__(self, ref: _Ref) -> None:
        self._ref: _Ref = ref
        self._token: str | None = None

    def manifest(self, reference: str) -> tuple[dict[str, Any], str]:
        body, headers = self._get(f"manifests/{reference}", _MANIFEST_TYPES)
        # A manifest's digest is the hash of its bytes, whatever a header says;
        # some registries (public ECR) don't send it.
        digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        sent = headers.get("Docker-Content-Digest")
        if sent is not None and sent != digest:
            raise RegistryError(
                f"{self._ref.repository}:{reference} says it is {sent} but hashes to {digest}"
            )
        if reference.startswith("sha256:") and digest != reference:
            raise RegistryError(
                f"{self._ref.repository}@{reference} came back as {digest}"
            )
        return json.loads(body), digest

    def blob(self, digest: str) -> dict[str, Any]:
        body, _ = self._get(f"blobs/{digest}", None)
        return json.loads(body)

    def _get(self, path: str, accept: str | None) -> tuple[bytes, Message]:
        url = f"https://{self._ref.registry}/v2/{self._ref.repository}/{path}"
        for attempt in range(_ATTEMPTS):
            headers = {"Accept": accept} if accept else {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=_TIMEOUT_S
                ) as response:
                    return response.read(), response.headers
            except urllib.error.HTTPError as e:
                if e.code == 401 and self._token is None:
                    self._token = _token(e.headers.get("WWW-Authenticate", ""))
                    continue
                if e.code == 429 or e.code >= 500:
                    if attempt + 1 < _ATTEMPTS:
                        time.sleep(_retry_after(e.headers, attempt))
                        continue
                raise RegistryError(f"{url}: HTTP {e.code}") from e
            except urllib.error.URLError as e:
                if attempt + 1 < _ATTEMPTS:
                    time.sleep(_retry_after(None, attempt))
                    continue
                raise RegistryError(f"{url}: {e.reason}") from e
        raise RegistryError(f"{url}: gave up after {_ATTEMPTS} attempts")


def _token(challenge: str) -> str:
    """An anonymous pull token, from the realm a 401's challenge names."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", None)
    if not challenge.lower().startswith("bearer") or realm is None:
        raise RegistryError(
            f"the registry wants credentials ({challenge!r}); only anonymous pulls are supported"
        )
    url = f"{realm}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:
        body = json.load(response)
    token = body.get("token") or body.get("access_token")
    if not token:
        raise RegistryError(f"{realm} gave no token")
    return token


def _retry_after(headers: Message | None, attempt: int) -> float:
    try:
        return min(float((headers or {}).get("Retry-After") or ""), 60.0)
    except ValueError:
        return min(2.0**attempt, 60.0)

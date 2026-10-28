"""Download contest attachments into a DOMjudge-style samples archive."""

import io
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlparse

from .client import QojError

MAX_FILES = 1000
MAX_BYTES = 64 * 1024 * 1024


def attachment_files(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise QojError("Attachment is not a ZIP archive") from exc
    files = {}
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES or sum(info.file_size for info in infos) > MAX_BYTES:
            raise QojError("Attachment exceeds safe extraction limits")
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename
            path = PurePosixPath(name)
            mode = info.external_attr >> 16
            if ("\\" in name or ":" in name or any(ord(char) < 32 for char in name)
                    or path.is_absolute() or ".." in path.parts
                    or stat.S_ISLNK(mode) or name in files):
                raise QojError("Attachment contains unsafe or duplicate paths")
            files[name] = archive.read(info)
    return files


def natural_key(name):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def organize(files):
    inputs = {name[:-3]: content for name, content in files.items() if name.lower().endswith(".in")}
    answers = {name[:-4]: content for name, content in files.items() if name.lower().endswith(".ans")}
    samples = {}
    for index, stem in enumerate(sorted(inputs, key=natural_key), 1):
        samples[f"{index}.in"] = inputs[stem]
        if stem in answers:
            samples[f"{index}.ans"] = answers[stem]
    paired = set(inputs) == set(answers)
    extra_files = [name for name in files if not name.lower().endswith((".in", ".ans"))]
    special = not inputs or not paired or bool(extra_files)
    return samples, special, len(inputs), sum(stem in answers for stem in inputs)


def download(client, contest):
    samples, extras, warnings = {}, {}, []
    for label, problem in contest["problems"].items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", label) or not str(problem["id"]).isdigit():
            raise QojError("Invalid problem mapping in contest configuration")
        query = urlencode({"type": "problem", "id": problem["id"], "contest_id": contest["id"]})
        response = client.get("/download.php?" + query, allow_404=True)
        if urlparse(response.url).path.startswith("/login"):
            raise QojError("Login required or session expired; run qoj login")
        if response.status_code == 404:
            warnings.append(f"{label}: no attachment (HTTP 404)")
            continue
        if len(response.content) > MAX_BYTES:
            raise QojError(f"{label}: attachment exceeds size limit")
        if not zipfile.is_zipfile(io.BytesIO(response.content)):
            if response.content.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                raise QojError(f"{label}: site returned HTML instead of an attachment")
            extras[f"attachment-extras/problem-{label}/attachment.bin"] = response.content
            warnings.append(f"{label}: non-ZIP attachment saved as extra")
            continue
        try:
            files = attachment_files(response.content)
            organized, special, count, pairs = organize(files)
        except (OSError, RuntimeError, zipfile.BadZipFile, QojError) as exc:
            raise QojError(f"{label}: invalid attachment: {exc}") from exc
        for name, content in organized.items():
            samples[f"samples-{label}/{name}"] = content
        if special:
            for name, content in files.items():
                extras[f"attachment-extras/problem-{label}/{name}"] = content
            warnings.append(f"{label}: {count} inputs, {pairs} paired answers; extra or incomplete attachment")
        print(f"{label}: {count} inputs, {pairs} answers")
    return samples, extras, warnings


def zip_bytes(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def write_archives(output, samples, extras, force=False):
    if not samples:
        raise QojError("No sample inputs found; no samples.zip was written")
    output = Path(output)
    if not output.is_dir():
        raise QojError(f"Output directory does not exist: {output}")
    targets = [(output / "samples.zip", samples)]
    if extras:
        targets.append((output / "attachment-extras.zip", extras))
    for path, _ in targets:
        if path.exists() and not force:
            raise QojError(f"{path} already exists; use --force to replace it")
    # Build all archives before opening existing output files.
    payloads = [(path, zip_bytes(files)) for path, files in targets]
    for path, payload in payloads:
        if not force:
            with path.open("xb") as stream:
                stream.write(payload)
            continue
        fd, name = tempfile.mkstemp(dir=output, prefix=".qoj-samples-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return [path for path, _ in targets]

import re
import wave
from pathlib import Path
from time import time

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from . import queue
from .db import write_transaction
from .schema import PLATFORMS, events, jobs, media_cache

DAY_MS = 86_400_000
ARTIFACT = re.compile(r"[0-9a-f]{32}-[1-9][0-9]*\.(media|wav|download|partial\.wav)")


def root(config):
    return config.workspace_dir / "media"


def resolve(config, relative):
    if not relative:
        return None
    path = config.workspace_dir / relative
    if (
        path.parent.parent != root(config) or path.parent.name not in PLATFORMS
        or not ARTIFACT.fullmatch(path.name)
        or path.resolve() != path
    ):
        return None
    return path


def load(connection, config, work_id, retention):
    row = connection.execute(
        select(media_cache).where(media_cache.c.work_id == work_id)
    ).mappings().first()
    if not row:
        return None, None
    threshold = queue.now_ms() - retention * DAY_MS

    def usable(kind):
        path = resolve(config, row[f"{kind}_path"])
        try:
            if path and row[f"{kind}_at"] >= threshold and path.stat().st_size:
                return path
        except OSError:
            pass
        return None

    audio = usable("audio")
    if audio:
        try:
            with wave.open(str(audio), "rb") as pcm:
                if (
                    (pcm.getnchannels(), pcm.getsampwidth(), pcm.getframerate()) != (1, 2, 16_000)
                    or pcm.getnframes() <= 0
                ):
                    audio = None
                else:
                    pcm.setpos(pcm.getnframes() - 1)
                    if len(pcm.readframes(1)) != 2:
                        audio = None
        except (OSError, wave.Error, EOFError):
            audio = None
    return usable("media"), audio


def record(connection, config, work_id, kind, path, at):
    values = {f"{kind}_path": path.relative_to(config.workspace_dir).as_posix(), f"{kind}_at": at}
    if kind == "media":
        values.update(audio_path=None, audio_at=None)
    statement = insert(media_cache).values(work_id=work_id, **values)
    connection.execute(statement.on_conflict_do_update(
        index_elements=[media_cache.c.work_id], set_=values,
    ))


def invalidate(connection, config, work_id, kind, path):
    path_col, time_col = media_cache.c[f"{kind}_path"], media_cache.c[f"{kind}_at"]
    connection.execute(media_cache.update().where(
        media_cache.c.work_id == work_id,
        path_col == path.relative_to(config.workspace_dir).as_posix(),
    ).values({path_col: None, time_col: None}))


def prune(engine, config, retention):
    at = queue.now_ms()
    with write_transaction(engine) as connection:
        connection.execute(events.delete().where(
            events.c.created_at < at - retention["log_days"] * DAY_MS,
            ~select(jobs.c.id).where(
                jobs.c.id == events.c.job_id, jobs.c.status.in_(("queued", "running")),
            ).exists(),
        ))
        if connection.scalar(select(jobs.c.id).where(
            jobs.c.kind.in_(("asr", "media_fetch")), jobs.c.status == "running",
        ).limit(1)):
            return
        threshold = at - retention["media_days"] * DAY_MS
        for kind in ("media", "audio"):
            path_col, time_col = media_cache.c[f"{kind}_path"], media_cache.c[f"{kind}_at"]
            expired = connection.execute(select(
                media_cache.c.work_id, path_col,
            ).where(time_col < threshold)).all()
            for work_id, relative in expired:
                path = resolve(config, relative)
                if path:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue
                connection.execute(media_cache.update().where(
                    media_cache.c.work_id == work_id,
                ).values({path_col: None, time_col: None}))
        connection.execute(media_cache.delete().where(
            media_cache.c.media_path.is_(None), media_cache.c.audio_path.is_(None),
        ))
        referenced = {
            path for row in connection.execute(select(
                media_cache.c.media_path, media_cache.c.audio_path,
            )) for path in row if path
        }
        for platform in PLATFORMS:
            directory = root(config) / platform
            if directory.resolve() != directory:
                continue
            for path in directory.glob("*"):
                relative = path.relative_to(config.workspace_dir).as_posix()
                if relative in referenced or not resolve(config, relative):
                    continue
                try:
                    age = time() - path.stat().st_mtime
                    limit = 3600 if path.name.endswith((".download", ".partial.wav")) else (
                        retention["media_days"] * DAY_MS / 1000
                    )
                    if age > limit:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass


def attempt_path(config, job, suffix) -> Path:
    directory = root(config) / job["platform"]
    if directory.resolve() != directory:
        raise OSError("Invalid media directory")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{job['id']}-{job['attempt_count']}.{suffix}"

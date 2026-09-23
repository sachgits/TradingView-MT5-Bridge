"""Watch-folder intake.

A producer - the Chrome extension, a script, or an operator saving a file - drops
a ``.json`` command into the inbox. This thread picks it up and hands it to
:class:`~bridge.service.Service`.

Two folders, one rule each:

**The inbox is read-only.** ``settings.signals_folder`` is shared with other
applications that produce and consume the same signals, so the bridge only lists,
stats and reads it. It never renames, writes, marks or deletes a file there, and
it keeps working when the inbox is mounted without write permission. Nothing in
this module opens an inbox path for writing.

**The data folder is ours.** Everything the bridge owns lives under
``settings.data_folder``: the SQLite ledger, ``intake/`` holding a copy of every
file consumed with the outcome written into the copy's extension
(``.processed``, ``.rejected``, ``.invalid``), and ``interrupted/`` for claims a
crash left open.

Because a handled file can no longer be renamed to mark it, the memory of what
was consumed lives in the ledger: ``file_intake`` is a permanent register keyed
on ``(source_path, content_hash)``, and inserting a row *is* the claim. The row
is written before the file is copied and before the command is queued, so a
crash at any point leaves either no claim (the file is picked up again on
restart) or an unresolved one (reported to an operator, never replayed - the
command may already have reached the EA).

Reading is still guarded against a producer that is mid-write: a file is skipped
until it is older than ``file_settle_ms``, and files whose size and mtime match
the register are skipped without being read at all, so an inbox that accumulates
history costs one ``stat`` per file per scan.

The inbox itself can be moved while the bridge runs - it is one of the settings on
the board. When it moves, every ``.json`` file already in the new folder is
*adopted*: recorded by path in ``inbox_adoption`` and never consumed, whatever its
content later becomes. Pointing the bridge at a folder that has been collecting
signals for months therefore queues nothing. A file written after the switch is a
normal command.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import contract, events
from .util import utc_now

Signature = Tuple[int, int, str]  # (size, mtime_ns, content_hash)


class FolderWatcher:
    """Single-threaded scanner driven by a wake-able sleep."""

    def __init__(
        self,
        service: Any,
        inbox: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        interrupted_dir: Optional[Path] = None,
    ) -> None:
        self.service = service
        self._inbox = Path(inbox) if inbox is not None else Path(service.settings.signals_folder)
        settings = getattr(service, "settings", None)
        self.data_dir = Path(data_dir) if data_dir is not None else Path(settings.intake_dir)
        self.interrupted_dir = (
            Path(interrupted_dir)
            if interrupted_dir is not None
            else Path(settings.interrupted_dir)
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # path -> (size, mtime_ns, content_hash) for files already consumed.
        # Seeded from the ledger so a restart does not re-read the inbox's
        # history, and consulted before every read so an unchanged file costs
        # one stat.
        self._known: Dict[str, Signature] = {}
        # Paths adopted when the inbox was switched. Never consumed, whatever
        # their content becomes.
        self._adopted: set = set()
        self._seeded = False
        # The inbox seen on the previous scan. None until the first one, which is
        # how a switch is told apart from a start.
        self._watched: Optional[str] = None
        self._state: Dict[str, Any] = {
            "running": False,
            "enabled": True,
            "folder": str(self._inbox),
            "folder_exists": False,
            "data_folder": str(self.data_dir.parent),
            "intake_dir": str(self.data_dir),
            "scans": 0,
            "files_seen": 0,
            "claimed": 0,
            "submit_failed": 0,
            "read_failed": 0,
            "duplicates_skipped": 0,
            "adopted_files": 0,
            "adopted_total": 0,
            "inbox_switches": 0,
            "copies": 0,
            "last_scan_at": None,
            "last_file_at": None,
            "last_error": None,
            "pending_files": 0,
            "interrupted_files": 0,
        }

    @property
    def inbox(self) -> Path:
        """The folder being watched, read live so the board can move it."""
        settings = getattr(self.service, "settings", None)
        folder = getattr(settings, "signals_folder", None) if settings is not None else None
        return Path(folder) if folder else self._inbox

    # Keep the old attribute name working: the folder being watched is the inbox.
    @property
    def folder(self) -> Path:
        return self.inbox

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bridge-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._set(running=False)

    def notify_settings_changed(self) -> None:
        """Break out of the sleep immediately after an interval change."""
        self._wake.set()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        # Reported live rather than from the last scan: the board changes this
        # setting, and showing the old folder until the next pass would make the
        # save look like it did nothing.
        state["folder"] = str(self.inbox)
        state["watch_interval"] = self.service.settings.watch_interval
        state["file_settle_ms"] = self.service.settings.file_settle_ms
        state["inbox_read_only"] = contract.INBOX_READ_ONLY
        return state

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._state[key] = self._state.get(key, 0) + amount

    # ------------------------------------------------------------------- loop
    def _run(self) -> None:
        self._set(running=True)
        self._recover_interrupted()
        while not self._stop.is_set():
            interval = max(0.1, float(self.service.settings.watch_interval))
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - a scanner must not die
                self._set(last_error=f"{type(exc).__name__}: {exc}", last_scan_at=utc_now())
            # Housekeeping runs on the watcher's clock so queued signals expire
            # and EA liveness is re-evaluated even when nothing is polling.
            try:
                self.service.tick()
            except Exception as exc:  # noqa: BLE001
                self._set(last_error=f"tick: {type(exc).__name__}: {exc}")
            self._wake.wait(interval)
            self._wake.clear()
        self._set(running=False)

    def scan_once(self) -> List[Dict[str, Any]]:
        """One read-only pass over the inbox. Returns what was newly consumed."""
        inbox = self.inbox
        if not self._seeded:
            self._seed_known()
        self._notice_inbox(inbox)
        if not self.service.settings.enable_file_intake:
            self._set(enabled=False, folder=str(inbox), folder_exists=inbox.is_dir())
            return []
        self._set(enabled=True, folder=str(inbox))

        if not inbox.is_dir():
            # Creating a missing inbox is a convenience for a first run. Failure
            # is not fatal: a read-only mount is a perfectly valid inbox, and the
            # bridge has no business insisting on being able to write here.
            try:
                inbox.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        self._set(folder_exists=inbox.is_dir())
        self._ensure_data_dirs()

        try:
            entries = sorted(p for p in inbox.iterdir() if p.is_file())
        except OSError as exc:
            self._set(last_error=f"cannot list inbox: {exc}", last_scan_at=utc_now())
            return []

        inbox_files = [p for p in entries if p.suffix == contract.FILE_SUFFIX_PENDING]
        now = time.time()
        settle = max(0, int(self.service.settings.file_settle_ms)) / 1000.0
        results: List[Dict[str, Any]] = []
        pending = 0
        adopted = 0

        for path in inbox_files:
            if self._stop.is_set():
                break
            if str(path) in self._adopted:
                # It was already there when we started watching this folder, so
                # it is history belonging to whoever else uses it - not a command
                # addressed to us. Skipped on content changes too: a shared inbox
                # is often updated in place by its own application.
                adopted += 1
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = self._known.get(str(path))
            if signature is not None and signature[:2] == (stat.st_size, stat.st_mtime_ns):
                # Already consumed, unchanged since. The inbox keeps its files,
                # so this is the common case and it must stay cheap.
                continue
            if now - stat.st_mtime < settle:
                pending += 1
                continue
            pending += 1
            result = self._consume(path, stat)
            if result is not None:
                pending -= 1
                results.append(result)

        self._set(
            scans=self._state["scans"] + 1,
            pending_files=pending,
            adopted_files=adopted,
            interrupted_files=self._interrupted_count(),
            last_scan_at=utc_now(),
        )
        if results:
            self._set(last_file_at=utc_now(), files_seen=self._state["files_seen"] + len(results))
        return results

    # -------------------------------------------------------------- the inbox
    def _notice_inbox(self, inbox: Path) -> None:
        """React to the inbox moving, which the board can do while we run.

        Everything already in the new folder is adopted rather than consumed.
        Without that, changing one text field would queue one trade per file in a
        folder that has been accumulating signals for months - and the operator
        who typed the path is the least likely person to expect it.

        The first scan is not a switch: a file dropped while the bridge was
        stopped is still a command, and adopting it would silently swallow the
        "start the bridge and it picks up what is waiting" workflow.
        """
        current = str(inbox)
        previous, self._watched = self._watched, current
        if previous is None or previous == current:
            return
        self._bump("inbox_switches")
        try:
            present = sorted(p for p in inbox.iterdir()
                             if p.is_file() and p.suffix == contract.FILE_SUFFIX_PENDING)
        except OSError as exc:
            self._set(last_error=f"cannot list inbox: {exc}")
            present = []
        entries = []
        for path in present:
            try:
                stat = path.stat()
                entries.append((str(path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                entries.append((str(path), None, None))
        try:
            added = self.service.store.adopt_files(current, entries)
        except Exception as exc:  # noqa: BLE001 - never trade what we meant to adopt
            self._set(last_error=f"cannot record adopted files: {exc}")
            self._adopted.update(str(path) for path in present)
            return
        self._adopted.update(str(path) for path in present)
        self._set(adopted_total=self.service.store.adoption_count())
        self.service.emit(
            events.WATCHER,
            None,
            {
                "file": None,
                "inbox": current,
                "previous_inbox": previous,
                "adopted": added,
                "reason": f"the inbox moved to {current}; {added} file(s) already there "
                          "were adopted and will not be traded",
            },
        )

    def _ensure_data_dirs(self) -> None:
        for folder in (self.data_dir, self.interrupted_dir):
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._set(last_error=f"cannot create {folder}: {exc}")

    def _interrupted_count(self) -> int:
        try:
            parked = sum(1 for p in self.interrupted_dir.iterdir() if p.is_file())
        except OSError:
            parked = 0
        return parked

    # ------------------------------------------------------------------ claim
    def _consume(self, path: Path, stat: os.stat_result) -> Optional[Dict[str, Any]]:
        """Read one inbox file, claim it in the ledger, copy it, submit it.

        The inbox file is only ever opened for reading. Every write below lands
        in the bridge's own data folder or in SQLite.
        """
        try:
            data = path.read_bytes()
        except OSError as exc:
            # Unreadable even after the settle window (antivirus, a share that
            # dropped). Leave it alone and let the next pass retry - dropping a
            # command on the floor is worse than reading it twice, and the
            # register makes a second read harmless.
            self._bump("read_failed")
            self._set(last_error=f"cannot read {path.name}: {exc}")
            return None

        digest = hashlib.sha256(data).hexdigest()
        existing = self.service.store.claim_file(
            source_path=str(path),
            content_hash=digest,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        if existing is not None:
            # Same bytes at the same path as a file already consumed. Rewriting a
            # file with identical content is not a new command, and re-queueing it
            # would be a second execution of the same trade.
            self._known[str(path)] = (stat.st_size, stat.st_mtime_ns, digest)
            self._bump("duplicates_skipped")
            return None

        self._bump("claimed")
        copy = self._copy_in(path.name, data)
        self._known[str(path)] = (stat.st_size, stat.st_mtime_ns, digest)

        text = data.decode("utf-8-sig", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            # The extension is .json, so the content must parse. It is NOT
            # offered to the legacy plain-text parser: a truncated file such as
            # '{"action": "buy", "sym' still contains the word buy, and the
            # legacy parser would happily turn that into a market order sized by
            # the EA's own inputs. A corrupt file must never become a trade.
            result = self.service.reject_intake(
                f"{path.name} is not valid JSON: {exc}",
                source=contract.SOURCE_FILE,
                source_file=path.name,
                raw_payload=text,
            )
            parked = self._park(copy, contract.FILE_SUFFIX_INVALID)
            self._resolve(path, digest, contract.STATUS_INVALID, result.get("request_id"), parked)
            return {"file": path.name, "marked": parked.name if parked else None, **result}

        try:
            result = self.service.submit(
                payload, contract.SOURCE_FILE, source_file=path.name
            )
        except Exception as exc:  # noqa: BLE001 - never leave a claim open
            self._bump("submit_failed")
            self._set(last_error=f"intake failed for {path.name}: {type(exc).__name__}: {exc}")
            parked = self._park(copy, contract.FILE_SUFFIX_INVALID)
            self._resolve(path, digest, contract.STATUS_INVALID, None, parked)
            return {"file": path.name, "ok": False, "status": contract.STATUS_INVALID,
                    "marked": parked.name if parked else None, "errors": [str(exc)]}

        status = result.get("status", contract.STATUS_INVALID)
        suffix = contract.FILE_SUFFIX_FOR_STATUS.get(status, contract.FILE_SUFFIX_REJECTED)
        parked = self._park(copy, suffix)
        self._resolve(path, digest, status, result.get("request_id"), parked)
        return {"file": path.name, "marked": parked.name if parked else None, **result}

    def _resolve(
        self,
        path: Path,
        digest: str,
        outcome: str,
        request_id: Optional[str],
        parked: Optional[Path],
    ) -> None:
        """Close the claim. Without this the file is reported as interrupted."""
        try:
            self.service.store.resolve_file_claim(
                source_path=str(path),
                content_hash=digest,
                outcome=outcome,
                request_id=request_id,
                copy_name=parked.name if parked is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            self._set(last_error=f"cannot record intake outcome for {path.name}: {exc}")
        if parked is not None:
            self._bump("copies")

    def _copy_in(self, name: str, data: bytes) -> Optional[Path]:
        """Copy the inbox file into our own folder as ``<stem>.processing``.

        The copy is what gets marked, so the inbox stays pristine. Failing to
        copy is not fatal: the claim is already in the ledger and the command
        still reaches the EA. Only the audit trail is thinner.
        """
        stem = Path(name).stem or "signal"
        target = self.data_dir / f"{stem}{contract.FILE_SUFFIX_PROCESSING}"
        attempt = 1
        while target.exists():
            attempt += 1
            if attempt > 999:
                # Never overwrite an existing record: fall back to a name that
                # cannot collide instead.
                target = self.data_dir / f"{stem}.{time.time_ns()}{contract.FILE_SUFFIX_PROCESSING}"
                break
            target = self.data_dir / f"{stem}.{attempt}{contract.FILE_SUFFIX_PROCESSING}"
        try:
            self._assert_ours(target)
            target.write_bytes(data)
            return target
        except OSError as exc:
            self._set(last_error=f"cannot copy {name} into {self.data_dir}: {exc}")
            return None

    def _assert_ours(self, target: Path) -> None:
        """Refuse to write anywhere outside the bridge's own folders.

        Cheap insurance for the one promise this module makes: the inbox is
        never written to, even if a future caller hands it a bad path.
        """
        resolved = target.resolve()
        allowed = {self.data_dir.resolve(), self.interrupted_dir.resolve()}
        if not any(folder in resolved.parents for folder in allowed):
            raise OSError(f"refusing to write {resolved} outside the bridge data folder")

    def _park(self, copy: Optional[Path], suffix: str) -> Optional[Path]:
        """Mark our copy with the outcome. A missing copy parks as nothing."""
        if copy is None or not copy.exists():
            return None
        target = copy.with_suffix(suffix)
        # Two files named the same way would collide. Suffix a counter rather
        # than overwriting a record.
        attempt = 1
        while True:
            try:
                self._assert_ours(target)
                os.rename(copy, target)
                return target
            except FileExistsError:
                attempt += 1
                target = copy.with_suffix(f"{suffix}.{attempt}")
                if attempt > 999:
                    raise
            except OSError as exc:
                self._set(last_error=f"cannot mark copy {copy.name}: {exc}")
                return copy

    # -------------------------------------------------------------- recovery
    def _recover_interrupted(self) -> None:
        """Report claims a previous crash left open, and set their copies aside.

        The bridge died between claiming a file and recording an outcome, so the
        command may or may not have reached the EA. Replaying it automatically is
        exactly the double execution the ledger exists to prevent, so the claim is
        closed as ``interrupted``, its copy is moved to ``interrupted/``, and an
        event tells the operator to check the board. The inbox file is untouched:
        it still says ``.json`` and simply will not be consumed again.
        """
        self._ensure_data_dirs()
        self._seed_known()

        for claim in self.service.store.unresolved_file_claims():
            source = claim.get("source_path") or "?"
            copy_name = claim.get("copy_name")
            parked: Optional[Path] = None
            if copy_name:
                parked = self._set_aside(self.data_dir / copy_name)
            self.service.store.resolve_file_claim(
                source_path=claim.get("source_path", ""),
                content_hash=claim.get("content_hash", ""),
                outcome=contract.INTAKE_OUTCOME_INTERRUPTED,
                copy_name=parked.name if parked is not None else copy_name,
            )
            self.service.emit(
                events.WATCHER,
                claim.get("request_id"),
                {
                    "file": Path(source).name,
                    "source_path": source,
                    "marked": parked.name if parked is not None else None,
                    "reason": "a previous run stopped while handling this file; "
                              "it was not replayed automatically",
                },
            )

        # A copy left in intake/ as .processing whose claim did get resolved is
        # stale; one whose claim is missing entirely is unknown. Either way it
        # belongs in interrupted/, not in the active intake folder.
        for leftover in sorted(self.data_dir.glob("*" + contract.FILE_SUFFIX_PROCESSING)):
            parked = self._set_aside(leftover)
            if parked is not None:
                self.service.emit(
                    events.WATCHER,
                    None,
                    {"file": parked.name, "marked": parked.name,
                     "reason": "an unfinished copy was found in the intake folder"},
                )

    def _set_aside(self, path: Path) -> Optional[Path]:
        try:
            if not path.exists():
                return None
            target = self.interrupted_dir / (path.stem + contract.FILE_SUFFIX_INTERRUPTED)
            attempt = 1
            while target.exists():
                attempt += 1
                target = self.interrupted_dir / f"{path.stem}.{attempt}{contract.FILE_SUFFIX_INTERRUPTED}"
                if attempt > 999:
                    break
            self._assert_ours(target)
            shutil.move(str(path), str(target))
            return target
        except OSError as exc:
            self._set(last_error=f"cannot set aside {path.name}: {exc}")
            return None

    def _seed_known(self) -> None:
        """Load the register into the stat cache so history is not re-read."""
        self._seeded = True
        try:
            self._known.update(self.service.store.known_file_signatures())
            self._adopted.update(self.service.store.adopted_paths())
            self._set(adopted_total=len(self._adopted))
        except Exception as exc:  # noqa: BLE001
            self._set(last_error=f"cannot load intake register: {exc}")

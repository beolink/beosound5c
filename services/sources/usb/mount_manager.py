"""MountManager -- auto-detect and mount BM5 and external music drives."""

import asyncio
import itertools
import json
import os
import sqlite3
import logging
import re
from pathlib import Path

from bm5_library import BM5Library
from file_browser import FileBrowser
from lib.file_playback import AUDIO_EXTENSIONS

log = logging.getLogger('beo-usb')

# Where auto-detected external drives get mounted (one subdir per drive)
EXTERNAL_MOUNT_BASE = "/media/beo-usb"
# Filesystems we know how to mount read-only for browsing
EXTERNAL_FSTYPES = {'vfat', 'exfat', 'ntfs', 'ntfs3', 'ext2', 'ext3', 'ext4'}


class MountManager:
    """Manages USB mount points from config. Auto-detects BM5 drives and
    any other USB-attached storage that contains music."""

    def __init__(self, paths):
        self.paths = paths or []
        self.mounts = []  # list of (name, browser) -- BM5Library or FileBrowser

    async def init(self):
        """Initialize mounts from configured paths, then auto-detect.

        A single unreadable path must never kill the service: a failing
        or yanked disk surfaces as OSError EIO from os.stat — pathlib's
        is_dir() only swallows does-not-exist errors, everything else
        propagates.  Treat such paths as "no USB present" and move on.
        """
        await self._cleanup_stale_external()

        for path in self.paths:
            try:
                browser = await self._init_path(path)
            except (OSError, sqlite3.Error) as e:
                log.warning("Skipping unreadable path %s: %s", path, e)
                continue
            if browser:
                name = getattr(browser, 'name', Path(path).name)
                self.mounts.append((name, browser))

        # No configured paths, or none worked — auto-detect BM5 HDD
        if not any(isinstance(b, BM5Library) for _, b in self.mounts):
            try:
                browser = await self._auto_detect_bm5()
            except (OSError, sqlite3.Error) as e:
                log.warning("USB auto-detect failed: %s", e)
                browser = None
            if browser:
                self.mounts.append((browser.name, browser))

        # Any other USB-attached drive with music on it — mount and expose
        # as a library, exactly as if its path were listed in the config
        try:
            await self._auto_mount_external()
        except Exception as e:
            log.warning("External drive auto-mount failed: %s", e)

    async def _init_path(self, path):
        """Initialize a single path as BM5Library or FileBrowser.
        If the path is an empty directory, try mounting an NTFS partition there."""
        p = Path(path)
        if not p.is_dir():
            log.warning("Path not found: %s", path)
            return None

        # Empty dir — likely an unmounted mount point
        if not any(p.iterdir()):
            if not await self._try_mount_to(path):
                log.warning("Empty mount point, no NTFS partition found: %s", path)
                return None

        # Check for BM5 drive
        if (p / "BM-Share" / "Music").is_dir():
            lib = BM5Library(path)
            if lib.available:
                lib.open()
                log.info("BM5 library at %s: %s", path, lib.name)
                return lib

        # Plain filesystem
        browser = FileBrowser(path)
        if browser.available:
            return browser
        return None

    async def _try_mount_to(self, target_path):
        """Try to mount an unmounted NTFS partition at target_path."""
        try:
            result = await asyncio.create_subprocess_exec(
                'lsblk', '--json', '-o', 'NAME,FSTYPE,MOUNTPOINT,SIZE',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await result.communicate()
            data = json.loads(stdout)
        except Exception as e:
            log.warning("lsblk failed: %s", e)
            return False

        candidates = []
        for dev in data.get('blockdevices', []):
            self._collect_ntfs(dev, candidates)

        for dev_name, mountpoint in candidates:
            if mountpoint:
                continue
            dev_path = f"/dev/{dev_name}"
            for fs in ('ntfs3', 'ntfs-3g'):
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'mount', '-t', fs, '-o', 'ro,uid=1000,gid=1000',
                    dev_path, target_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode == 0:
                    log.info("Mounted %s at %s (%s)", dev_path, target_path, fs)
                    return True
        return False

    async def _auto_detect_bm5(self):
        """Scan block devices for NTFS partition with BM5 marker."""
        mount_target = "/mnt/beo-usb-auto"

        # Check if already mounted at target from a previous run
        try:
            already_mounted = (Path(mount_target).is_dir()
                               and (Path(mount_target) / "BM-Share" / "Music").is_dir())
        except OSError as e:
            # Stale mount from a failing/yanked disk (EIO on stat).
            # Lazy-unmount it so a later re-plug can mount cleanly,
            # then fall through to fresh block-device detection.
            log.warning("Mount at %s unreadable (%s) — releasing stale mount",
                        mount_target, e)
            proc = await asyncio.create_subprocess_exec(
                'sudo', 'umount', '-l', mount_target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            already_mounted = False
        if already_mounted:
            lib = BM5Library(mount_target)
            if lib.available:
                lib.open()
                log.info("BM5 already mounted at %s", mount_target)
                return lib

        try:
            result = await asyncio.create_subprocess_exec(
                'lsblk', '--json', '-o', 'NAME,FSTYPE,MOUNTPOINT,SIZE',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await result.communicate()
            data = json.loads(stdout)
        except Exception as e:
            log.warning("lsblk failed: %s", e)
            return None

        candidates = []
        for dev in data.get('blockdevices', []):
            self._collect_ntfs(dev, candidates)

        for dev_name, mountpoint in candidates:
            # Already mounted somewhere — check for BM5 marker
            if mountpoint:
                if (Path(mountpoint) / "BM-Share" / "Music").is_dir():
                    lib = BM5Library(mountpoint)
                    if lib.available:
                        lib.open()
                        log.info("BM5 found at existing mount: %s", mountpoint)
                        return lib
                continue

            # Probe unmounted partition
            dev_path = f"/dev/{dev_name}"
            tmp_mount = f"/tmp/beo-usb-probe-{dev_name}"
            try:
                Path(tmp_mount).mkdir(parents=True, exist_ok=True)
                mounted = False
                for fs in ('ntfs3', 'ntfs-3g'):
                    proc = await asyncio.create_subprocess_exec(
                        'sudo', 'mount', '-t', fs, '-o', 'ro',
                        dev_path, tmp_mount,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                    if proc.returncode == 0:
                        mounted = True
                        break
                if not mounted:
                    continue

                if (Path(tmp_mount) / "BM-Share" / "Music").is_dir():
                    # Found BM5 — remount at permanent location
                    await self._umount(tmp_mount)
                    await asyncio.create_subprocess_exec(
                        'sudo', 'mkdir', '-p', mount_target,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    for fs in ('ntfs3', 'ntfs-3g'):
                        proc = await asyncio.create_subprocess_exec(
                            'sudo', 'mount', '-t', fs, '-o', 'ro',
                            dev_path, mount_target,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await proc.wait()
                        if proc.returncode == 0:
                            lib = BM5Library(mount_target)
                            if lib.available:
                                lib.open()
                                log.info("BM5 auto-mounted: %s -> %s", dev_path, mount_target)
                                return lib
                else:
                    await self._umount(tmp_mount)
            except Exception as e:
                log.warning("Probe failed for %s: %s", dev_path, e)
            finally:
                try:
                    Path(tmp_mount).rmdir()
                except OSError:
                    pass

        return None

    async def _umount(self, path):
        await asyncio.create_subprocess_exec(
            'sudo', 'umount', path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    def _collect_ntfs(self, dev, candidates):
        """Recursively collect NTFS partitions from lsblk output."""
        fstype = dev.get('fstype', '')
        if fstype and 'ntfs' in fstype.lower():
            candidates.append((dev['name'], dev.get('mountpoint')))
        for child in dev.get('children', []):
            self._collect_ntfs(child, candidates)

    # ── External drive auto-mount ─────────────────────────────────────────

    async def _lsblk_external(self):
        """Return partitions on USB-attached disks: [(part_dict, disk_dict)].

        The transport is reported on the disk node, not the partition —
        filtering on tran == 'usb' keeps the system SD/NVMe out."""
        try:
            result = await asyncio.create_subprocess_exec(
                'lsblk', '--json', '-o', 'NAME,FSTYPE,MOUNTPOINT,SIZE,LABEL,TRAN,TYPE',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await result.communicate()
            data = json.loads(stdout)
        except Exception as e:
            log.warning("lsblk failed: %s", e)
            return []

        parts = []
        for disk in data.get('blockdevices', []):
            if disk.get('tran') != 'usb':
                continue
            # Partitionless drives carry the filesystem on the disk node
            for node in (disk.get('children') or [disk]):
                fstype = (node.get('fstype') or '').lower()
                if fstype in EXTERNAL_FSTYPES:
                    parts.append(node)
        return parts

    async def _auto_mount_external(self):
        """Mount every USB-attached music drive and expose it as a library.

        Anything already covered — configured paths, the BM5 auto-mount,
        system mounts — is skipped. Drives without audio files are left
        alone so a backup disk doesn't pop up as an empty music library."""
        covered = {str(getattr(b, 'root', '')) for _, b in self.mounts}
        covered |= {str(Path(p)) for p in self.paths}
        covered.add("/mnt/beo-usb-auto")

        for part in await self._lsblk_external():
            mountpoint = part.get('mountpoint')
            if mountpoint and (mountpoint == '/'
                               or mountpoint.startswith(('/boot', '/var', '/usr'))):
                continue
            if mountpoint and str(Path(mountpoint)) in covered:
                continue
            we_mounted = False
            if not mountpoint:
                mountpoint = await self._mount_external(part)
                if not mountpoint:
                    continue
                we_mounted = True
            root = Path(mountpoint)
            skip_reason = None
            try:
                if (root / "BM-Share" / "Music").is_dir():
                    skip_reason = "BM5 drive (handled separately)"
                elif self._is_os_partition(root):
                    # OS partitions carry system sounds (Windows Media/*.wav)
                    # that would pass the audio probe
                    skip_reason = "OS partition"
                elif not self._has_audio(root):
                    skip_reason = "no audio files"
            except OSError as e:
                skip_reason = f"unreadable ({e})"
            if skip_reason:
                log.info("External partition %s skipped — %s",
                         part.get('label') or part['name'], skip_reason)
                # Release skipped partitions under our mount base — whether
                # from this pass or left over from an earlier one — so
                # /media/beo-usb holds exactly the adopted libraries
                if we_mounted or mountpoint.startswith(EXTERNAL_MOUNT_BASE + "/"):
                    await self._release_mount(mountpoint)
                continue
            browser = FileBrowser(mountpoint)
            if browser.available:
                browser.name = str(part.get('label') or part['name'])
                self.mounts.append((browser.name, browser))
                log.info("External drive added as library: %s (%s)",
                         browser.name, mountpoint)
            elif we_mounted:
                await self._release_mount(mountpoint)

    @staticmethod
    def _is_os_partition(root):
        """True for OS/system partitions that should never be a music library.

        Case-insensitive: NTFS is case-insensitive under Windows but the
        ntfs3 driver is not, and e.g. the BM5's embedded-XP partition uses
        ``WINDOWS/system32``."""
        try:
            entries = {e.name.lower(): e
                       for e in itertools.islice(root.iterdir(), 400)}
        except OSError:
            return False
        # Windows install (BM5 drives carry an embedded-XP partition)
        if 'pagefile.sys' in entries or 'documents and settings' in entries:
            return True
        win = entries.get('windows')
        if win is not None and win.is_dir():
            try:
                if any(c.name.lower() == 'system32'
                       for c in itertools.islice(win.iterdir(), 400)):
                    return True
            except OSError:
                pass
        # macOS system volume
        if (root / 'System' / 'Library' / 'CoreServices').is_dir():
            return True
        # Linux rootfs
        if (root / 'etc' / 'fstab').is_file() and (root / 'usr').is_dir():
            return True
        return False

    async def _release_mount(self, mountpoint):
        """Unmount and remove a probe mount we created but didn't adopt."""
        proc = await asyncio.create_subprocess_exec(
            'sudo', 'umount', mountpoint,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        proc = await asyncio.create_subprocess_exec(
            'sudo', 'rmdir', mountpoint,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

    async def _mount_external(self, part):
        """Mount a partition read-only under EXTERNAL_MOUNT_BASE. Returns
        the mountpoint or None."""
        dev_path = f"/dev/{part['name']}"
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_',
                      str(part.get('label') or part['name'])).strip('_')
        target = f"{EXTERNAL_MOUNT_BASE}/{safe or part['name']}"

        proc = await asyncio.create_subprocess_exec(
            'sudo', 'mkdir', '-p', target,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

        fstype = (part.get('fstype') or '').lower()
        uid_opts = f"ro,uid={os.getuid()},gid={os.getgid()}"
        if 'ntfs' in fstype:
            attempts = [('ntfs3', uid_opts), ('ntfs-3g', uid_opts)]
        elif fstype in ('vfat', 'exfat'):
            attempts = [(fstype, uid_opts)]
        else:  # ext2/3/4 — no uid mapping, plain read-only
            attempts = [(fstype, 'ro')]

        for fs, opts in attempts:
            proc = await asyncio.create_subprocess_exec(
                'sudo', 'mount', '-t', fs, '-o', opts, dev_path, target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                log.info("Mounted %s at %s (%s)", dev_path, target, fs)
                return target
        log.warning("Could not mount %s (%s)", dev_path, fstype)
        return None

    def _has_audio(self, root, max_depth=3):
        """Shallow breadth-first probe for audio files. Bounded so a huge
        non-music disk can't stall the rescan."""
        queue = [(root, 0)]
        visited = 0
        while queue and visited < 60:
            d, depth = queue.pop(0)
            visited += 1
            try:
                entries = list(itertools.islice(d.iterdir(), 800))
            except OSError:
                continue
            for e in entries:
                if e.name.startswith('.'):
                    continue
                try:
                    if e.is_file() and e.suffix.lower() in AUDIO_EXTENSIONS:
                        return True
                    if e.is_dir() and depth < max_depth:
                        queue.append((e, depth + 1))
                except OSError:
                    continue
        return False

    async def _cleanup_stale_external(self):
        """Release auto-mounts whose drive was yanked (EIO on stat) and
        remove leftover empty mount dirs so re-plugs mount cleanly."""
        base = Path(EXTERNAL_MOUNT_BASE)
        try:
            if not base.is_dir():
                return
            children = list(base.iterdir())
        except OSError:
            return
        for d in children:
            stale = False
            try:
                if not any(d.iterdir()):
                    stale = True  # empty — unmounted leftover or dead mount
            except OSError:
                stale = True  # EIO — device gone underneath the mount
            if stale:
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'umount', '-l', str(d),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'rmdir', str(d),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()
                log.info("Released stale external mount: %s", d)

    @property
    def available(self):
        return len(self.mounts) > 0

    def get_mount(self, index):
        if 0 <= index < len(self.mounts):
            return self.mounts[index]
        return None, None

    def find_bm5(self, index=0):
        """Find the first (or nth) BM5Library mount."""
        count = 0
        for name, browser in self.mounts:
            if isinstance(browser, BM5Library):
                if count == index:
                    return browser
                count += 1
        return None

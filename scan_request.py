"""
scan_request.py — Input value object shared by the regular scan and Compare Scan.

Stage 3 of projects/ImageDeduper/docs/refactor-plan-2026-07.md.

WHY
---
The two scan workers took different positional/keyword arguments for what is
fundamentally the same request: which folder(s) to hash, where output goes, which
settings apply, and how much to trust the library cache. Giving both one input
shape is the prerequisite for merging their bodies (Stage 4), and it puts two
rules that were previously duplicated (and therefore driftable) in one place:

1. ``LibraryOptions.effective_trust`` -- staleness checks may only be skipped
   when the user chose Library mode AND enabled "trust library". Previously
   spelled ``trust_library and use_library`` in the regular worker and
   ``trust_main and use_lib_main`` in the compare worker.
2. ``frozen_out_folder`` -- the output folder captured when the scan STARTED.
   Reading it live at apply-time is the defect behind #159/#161 and #2149;
   holding it on the request makes the scan-time value the only one available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

MODE_SINGLE = "single"
MODE_COMPARE = "compare"


@dataclass(frozen=True)
class LibraryOptions:
    """Per-folder library-cache options.

    The cache itself is always loaded (see ``library.load_scan_cache``); these
    flags only decide whether cached entries are trusted without a per-file
    staleness check.
    """

    use: bool = False
    trust: bool = False

    @property
    def effective_trust(self) -> bool:
        """True only when Library mode is active AND trust is enabled.

        Browse mode must never bypass staleness checks, or a moved/edited file
        would be matched against a stale hash.
        """
        return bool(self.trust and self.use)


@dataclass
class ScanRequest:
    """Everything a scan run needs, captured at start time.

    Construct via :meth:`single` or :meth:`compare` rather than directly, so the
    mode-specific folder fields cannot be left inconsistent.
    """

    mode: str
    out_folder: Path
    settings: Any

    # single mode
    src: Optional[Path] = None

    # compare mode: main is the reference (never trashed), check holds candidates
    main_folder: Optional[Path] = None
    check_folder: Optional[Path] = None

    # library options — `primary` is `src` in single mode, `main_folder` in compare
    primary_library: LibraryOptions = field(default_factory=LibraryOptions)
    check_library: LibraryOptions = field(default_factory=LibraryOptions)

    resume_state: Any = None

    def __post_init__(self) -> None:
        self.out_folder = Path(self.out_folder)
        if self.mode == MODE_SINGLE:
            if self.src is None:
                raise ValueError("single-mode ScanRequest requires src")
            self.src = Path(self.src)
        elif self.mode == MODE_COMPARE:
            if self.main_folder is None or self.check_folder is None:
                raise ValueError(
                    "compare-mode ScanRequest requires main_folder and check_folder"
                )
            self.main_folder = Path(self.main_folder)
            self.check_folder = Path(self.check_folder)
        else:
            raise ValueError(f"unknown scan mode: {self.mode!r}")

    # ── constructors ─────────────────────────────────────────────────────────

    @classmethod
    def single(cls, src, out_folder, settings, *, use_library: bool = False,
               trust_library: bool = False, resume_state: Any = None) -> "ScanRequest":
        return cls(
            mode=MODE_SINGLE,
            src=src,
            out_folder=out_folder,
            settings=settings,
            primary_library=LibraryOptions(use=use_library, trust=trust_library),
            resume_state=resume_state,
        )

    @classmethod
    def compare(cls, main_folder, check_folder, out_folder, settings, *,
                use_lib_main: bool = False, trust_main: bool = False,
                use_lib_check: bool = False, trust_check: bool = False,
                resume_state: Any = None) -> "ScanRequest":
        return cls(
            mode=MODE_COMPARE,
            main_folder=main_folder,
            check_folder=check_folder,
            out_folder=out_folder,
            settings=settings,
            primary_library=LibraryOptions(use=use_lib_main, trust=trust_main),
            check_library=LibraryOptions(use=use_lib_check, trust=trust_check),
            resume_state=resume_state,
        )

    # ── derived values ───────────────────────────────────────────────────────

    @property
    def is_compare(self) -> bool:
        return self.mode == MODE_COMPARE

    @property
    def frozen_out_folder(self) -> str:
        """The output folder as of scan start.

        Consumers (report viewer, accept-and-move, manual trash) must use this
        rather than re-reading the UI field, which the user may have edited
        since the scan ran.
        """
        return str(self.out_folder)

    @property
    def primary_folder(self) -> Path:
        """The folder hashed first: ``src`` in single mode, ``main_folder`` in
        compare mode."""
        return self.main_folder if self.is_compare else self.src  # type: ignore[return-value]

    @property
    def folders_to_hash(self) -> "list[tuple[Path, LibraryOptions]]":
        """Folders this request hashes, in order, with their library options.

        Single mode yields one entry; compare mode yields main then check. This
        is the seam Stage 4a uses to run one collect implementation for both.
        """
        if self.is_compare:
            return [
                (self.main_folder, self.primary_library),    # type: ignore[list-item]
                (self.check_folder, self.check_library),     # type: ignore[list-item]
            ]
        return [(self.src, self.primary_library)]            # type: ignore[list-item]

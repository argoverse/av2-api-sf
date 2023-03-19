"""PyTorch data-loader for the scene flow task."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property
from typing import Optional, Tuple

import pandas as pd
from torch.utils.data import Dataset

import av2._r as rust
from av2.utils.typing import PathType

from .utils import Sweep

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SceneFlowDataloader(Dataset[Tuple[Sweep, Optional[Sweep]]]):
    """PyTorch data-loader for the sensor dataset.

    Args:
        root_dir: Path to the dataset directory.
        dataset_name: Dataset name (e.g., "av2").
        split_name: Name of the dataset split (e.g., "train").
        num_accumulated_sweeps: Number of temporally accumulated sweeps (accounting for ego-vehicle motion).
        memory_mapped: Boolean flag indicating whether to memory map the dataframes.
    """

    root_dir: PathType
    dataset_name: str
    split_name: str
    num_accumulated_sweeps: int = 1
    memory_mapped: bool = False

    _backend: rust.Dataloader = field(init=False)
    _current_idx: int = 0

    def __post_init__(self) -> None:
        """Initialize Rust backend."""
        self._backend = rust.Dataloader(
            str(self.root_dir),
            self.dataset_name,
            "sensor",
            self.split_name,
            self.num_accumulated_sweeps,
            self.memory_mapped,
        )

    @cached_property
    def file_index(self) -> pd.DataFrame:
        """File index dataframe composed of (log_id, timestamp_ns)."""
        return self._backend.file_index.to_pandas()

    def __getitem__(self, index: int) -> Tuple[Sweep, Optional[Sweep]]:
        """Get a pair of sweeps for scene flow computation.

        Args:
            index: Index in [0, num_sweeps - 1].

        Returns:
            Current sweep and the next sweep (if it exists).
        """
        sweep = self._backend.get(index)
        next_sweep = None

        next_index = index + 1
        if next_index < len(self):
            candidate_log_id: str = self.file_index["log_id"][next_index]
            current_log_id = sweep.sweep_uuid[0]
            if candidate_log_id == current_log_id:
                next_sweep = Sweep.from_rust(self._backend.get(next_index))
        return Sweep.from_rust(sweep), next_sweep

    def __len__(self) -> int:
        """Length of the data-loader."""
        return self._backend.__len__()

    def __iter__(self) -> SceneFlowDataloader:
        """Iterate method for the data-loader."""
        return self

    def __next__(self) -> Tuple[Sweep, Optional[Sweep]]:
        """Return a tuple of sweeps for scene flow."""
        if self._current_idx >= self.__len__():
            raise StopIteration
        datum = self.__getitem__(self._current_idx)
        self._current_idx += 1
        return datum
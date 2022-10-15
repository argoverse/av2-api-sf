"""Pytorch dataloader for the Argoverse 2 dataset."""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from enum import Enum, unique
from math import inf
from pathlib import Path
from typing import Any, Final, ItemsView, List, Optional, Tuple

import joblib
import numpy as np
import polars as pl
from filelock import FileLock
from torch.utils.data import Dataset
from upath import UPath

from av2.geometry.geometry import quat_to_mat
from av2.utils.typing import NDArrayFloat, PathType

from .utils import QUAT_WXYZ_FIELDS, Annotations, Lidar, Sweep, prevent_fsspec_deadlock, query_pose, read_feather

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

XYZ_FIELDS: Final[Tuple[str, str, str]] = ("x", "y", "z")
LIDAR_GLOB_PATTERN: Final[str] = "sensors/lidar/*.feather"

pl.Config.with_columns_kwargs = True


@unique
class FileCachingMode(str, Enum):
    """File caching mode."""

    DISK = "DISK"


@dataclass
class Av2(Dataset[Sweep]):
    """Pytorch dataloader for the sensor dataset.

    Args:
        root_dir: Path to the dataset directory.
        dataset_name: Dataset name.
        split_name: Name of the dataset split.
        min_annotation_range: Min Euclidean distance between the egovehicle origin and the annotation cuboid centers.
        max_annotation_range: Max Euclidean distance between the egovehicle origin and the annotation cuboid centers.
        min_lidar_range: Min Euclidean distance between the egovehicle origin and the lidar points.
        max_lidar_range: Max Euclidean distance between the egovehicle origin and the lidar points.
        num_accumulated_sweeps: Number of temporally accumulated sweeps (accounting for egovehicle motion).
        file_caching_mode: File caching mode.
    """

    root_dir: PathType
    dataset_name: str
    split_name: str
    min_annotation_range: float = 0.0
    max_annotation_range: float = inf
    min_lidar_range: float = 0.0
    max_lidar_range: float = inf
    min_interior_pts: int = 0
    num_accumulated_sweeps: int = 1
    file_caching_mode: Optional[FileCachingMode] = None
    file_index: List[Tuple[str, int]] = field(init=False)
    with_annotations: bool = False

    def __post_init__(self) -> None:
        """Build the file index."""
        prevent_fsspec_deadlock()
        self._build_file_index()
        self._log_dataloader_configuration()

    def __repr__(self) -> str:
        """Dataloader info."""
        info = "Dataloader configuration settings:\n"
        for key, value in sorted(self.items()):
            if key == "file_index":
                continue
            info += f"\t{key}: {value}\n"
        return info

    def items(self) -> ItemsView[str, Any]:
        """Return the attribute_name, attribute pairs for the dataloader."""
        return self.__dict__.items()

    @property
    def file_caching_dir(self) -> PathType:
        """File caching directory."""
        return Path("/") / "tmp" / "cache" / "av2" / self.dataset_name / self.split_name

    @property
    def split_dir(self) -> PathType:
        """Sensor dataset split directory."""
        return UPath(self.root_dir) / self.dataset_name / self.split_name

    def _log_dataloader_configuration(self) -> None:
        """Log the dataloader configuration."""
        info = "Dataloader has been configured. Here are the settings:\n"
        for key, value in self.items():
            if key == "file_index":
                continue
            info += f"\t{key}: {value}\n"
        logger.info("%s", info)

    def annotations_path(self, log_id: str) -> PathType:
        """Get the annotations at the specified log id.

        Args:
            log_id: Unique log identifier.

        Returns:
            Annotations path for the entire log.
        """
        return self.split_dir / log_id / "annotations.feather"

    def lidar_path(self, log_id: str, timestamp_ns: int) -> PathType:
        """Get the lidar path at the specified log id and timestamp.

        Args:
            log_id: Unique log identifier.
            timestamp_ns: Lidar timestamp in nanoseconds.

        Returns:
            Lidar path at the log id and timestamp.
        """
        return self.split_dir / log_id / "sensors" / "lidar" / f"{timestamp_ns}.feather"

    def pose_path(self, log_id: str) -> PathType:
        """Get the city egopose path."""
        return self.split_dir / log_id / "city_SE3_egovehicle.feather"

    def sweep_uuid(self, index: int) -> Tuple[str, int]:
        """Get the sweep uuid at the given index.

        Args:
            index: Dataset index.

        Returns:
            The sweep uuid (log_id, timestamp_ns).
        """
        return self.file_index[index]

    def __getitem__(self, index: int) -> Sweep:
        """Get the annotations and lidar for one sweep.

        Args:
            index: Dataset index.

        Returns:
            Sweep object containing annotations and lidar.
        """
        annotations = None
        if self.with_annotations:
            annotations = self.read_annotations(index)

        lidar = self.read_lidar(index)
        sweep_uuid = self.sweep_uuid(index)
        return Sweep(annotations=annotations, lidar=lidar, sweep_uuid=sweep_uuid)

    def _build_file_index(self) -> None:
        """Build the file index for the dataset."""
        file_cache_path = self.file_caching_dir.parent / f"file_index_{self.split_name}.feather"
        if file_cache_path.exists():
            file_index = pl.read_ipc(file_cache_path).to_numpy().tolist()
        else:
            logger.info("Building file index. This may take a moment ...")
            log_dirs = sorted(self.split_dir.glob("*"))
            path_lists: Optional[List[List[Tuple[str, int]]]] = joblib.Parallel(n_jobs=-1, backend="multiprocessing")(
                joblib.delayed(Av2._file_index_helper)(log_dir, LIDAR_GLOB_PATTERN) for log_dir in log_dirs
            )
            logger.info("File indexing complete.")
            if path_lists is None:
                raise RuntimeError("Error scanning the dataset directory!")
            if len(path_lists) == 0:
                raise RuntimeError("No file paths found. Please validate `self.dataset_dir` and `self.split_name`.")

            file_index = sorted(itertools.chain.from_iterable(path_lists))
            self.file_caching_dir.mkdir(parents=True, exist_ok=True)
            dataframe = pl.DataFrame(file_index, columns=["log_id", "timestamp_ns"])
            dataframe.write_ipc(file_cache_path)
        self.file_index = file_index

    def read_annotations(self, index: int) -> Annotations:
        """Read the sweep annotations.

        Args:
            index: Dataset index.

        Returns:
            The annotations object.
        """
        log_id, timestamp_ns = self.sweep_uuid(index)
        cache_path = self.file_caching_dir / log_id / "annotations.feather"

        if self.file_caching_mode == FileCachingMode.DISK and cache_path.exists():
            dataframe = read_feather(cache_path)
        else:
            annotations_path = self.annotations_path(log_id)
            dataframe = read_feather(annotations_path)
            dataframe = self._populate_annotations_velocity(index, dataframe)

        if self.file_caching_mode == FileCachingMode.DISK and not cache_path.exists():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            dataframe.write_ipc(cache_path)

        distance = np.linalg.norm(dataframe.select(pl.col(["tx_m", "ty_m", "tz_m"])).to_numpy(), axis=-1)
        dataframe = pl.concat([dataframe, pl.from_numpy(distance, columns=["distance"])], how="horizontal")
        dataframe = dataframe.filter(
            (pl.col("num_interior_pts") > self.min_interior_pts)
            & (pl.col("timestamp_ns") == timestamp_ns)
            & (pl.col("distance") >= self.min_annotation_range)
            & (pl.col("distance") <= self.max_annotation_range)
        )
        annotations = Annotations(dataframe)
        return annotations

    def _populate_annotations_velocity(self, index: int, annotations: pl.DataFrame) -> pl.DataFrame:
        """Populate the annotations with their estimated velocities.

        Args:
            index: Dataset index.
            annotations: DataFrame of annotations loaded from a feather file.

        Returns:
            The dataFrame populated with velocities.
        """
        current_log_id, _ = self.sweep_uuid(index)
        pose_path = self.pose_path(current_log_id)
        city_SE3_ego = read_feather(pose_path)

        annotations = annotations.sort(["track_uuid", "timestamp_ns"]).with_row_count()
        annotations = annotations.with_columns(row_nr=pl.col("row_nr").cast(pl.Int64))

        annotations_with_poses = annotations.select(
            [pl.col("timestamp_ns"), pl.col(["tx_m", "ty_m", "tz_m"]).map_alias(lambda x: f"{x}_obj")]
        ).join(city_SE3_ego, on="timestamp_ns")
        mats = quat_to_mat(annotations_with_poses.select(pl.col(list(QUAT_WXYZ_FIELDS))).to_numpy())
        translation = annotations_with_poses.select(pl.col(["tx_m", "ty_m", "tz_m"])).to_numpy()

        t_xyz = annotations_with_poses.select(pl.col(["tx_m_obj", "ty_m_obj", "tz_m_obj"])).to_numpy()
        t_xyz_city = pl.from_numpy(
            (t_xyz[:, None] @ mats.transpose(0, 2, 1) + translation[:, None]).squeeze(),
            ["tx_m_city", "ty_m_city", "tz_m_city"],
        )

        annotations_city = pl.concat(
            [annotations.select(pl.col(["row_nr", "timestamp_ns", "track_uuid"])), t_xyz_city],
            how="horizontal",
        )

        velocities = annotations_city.groupby_rolling(
            index_column="row_nr", period="3i", offset="-2i", by=["track_uuid"], closed="right"
        ).agg(
            [
                (pl.col("tx_m_city").diff() / (pl.col("timestamp_ns").diff() * 1e-9)).mean().alias("vx_m"),
                (pl.col("ty_m_city").diff() / (pl.col("timestamp_ns").diff() * 1e-9)).mean().alias("vy_m"),
                (pl.col("tz_m_city").diff() / (pl.col("timestamp_ns").diff() * 1e-9)).mean().alias("vz_m"),
            ]
        )
        annotations = annotations.join(velocities, on=["track_uuid", "row_nr"])
        return annotations.drop("row_nr")

    def read_lidar(self, index: int) -> Lidar:
        """Read the lidar sweep.

        Args:
            index: Dataset index.

        Returns:
            Tensor of annotations.
        """
        log_id, timestamp_ns = self.sweep_uuid(index)
        window = self.file_index[max(index - self.num_accumulated_sweeps + 1, 0) : index + 1][::-1]
        filtered_window: List[Tuple[str, int]] = list(filter(lambda sweep_uuid: sweep_uuid[0] == log_id, window))

        dataframe_list = []
        if len(window) > 0:
            poses = self._read_frame(
                src_path=self.pose_path(log_id),
                file_caching_path=self.file_caching_dir / log_id / "city_SE3_egovehicle.feather",
            )
            ego_current_SE3_city = query_pose(poses, timestamp_ns).inverse()
            for _, (log_id, timestamp_ns_k) in enumerate(filtered_window):
                dataframe = self._read_frame(
                    src_path=self.lidar_path(log_id, timestamp_ns_k),
                    file_caching_path=self.file_caching_dir
                    / log_id
                    / "sensors"
                    / "lidar"
                    / f"{timestamp_ns_k}.feather",
                )

                points_past: NDArrayFloat = dataframe.select(pl.col(list(XYZ_FIELDS))).to_numpy()
                # Timestamps do not match, we're likely in a new reference frame.
                timedelta = timestamp_ns - timestamp_ns_k
                if timedelta > 0:
                    city_SE3_ego_past = query_pose(poses, timestamp_ns_k)
                    ego_current_SE3_ego_past = ego_current_SE3_city.compose(city_SE3_ego_past)
                    points_ego_current = ego_current_SE3_ego_past.transform_point_cloud(points_past)
                else:
                    points_ego_current = points_past

                dataframe = pl.concat(
                    [
                        pl.from_numpy(points_ego_current.astype(np.float32), XYZ_FIELDS),
                        dataframe.select(pl.col("*").exclude(XYZ_FIELDS)),
                        pl.from_numpy(np.full(len(points_ego_current), fill_value=timedelta), columns=["timedelta_ns"]),
                    ],
                    how="horizontal",
                )
                dataframe_list.append(dataframe)
        dataframe = pl.concat(dataframe_list)
        dataframe = self._post_process_lidar(dataframe)
        return Lidar(dataframe)

    def _post_process_lidar(self, dataframe: pl.DataFrame) -> pl.DataFrame:
        """Apply post-processing operations on the point cloud.

        Args:
            dataframe: Lidar dataframe.

        Returns:
            The filtered lidar dataframe.
        """
        distance = np.linalg.norm(dataframe.select(pl.col(list(XYZ_FIELDS))).to_numpy(), axis=-1)
        dataframe_distance = pl.from_numpy(distance, columns=["distance"])
        dataframe = pl.concat([dataframe, dataframe_distance], how="horizontal")
        dataframe = dataframe.filter(
            (pl.col("distance") >= self.min_lidar_range) & (pl.col("distance") <= self.max_lidar_range)
        )
        dataframe = dataframe.sort(["timedelta_ns", "distance"])
        return dataframe

    @staticmethod
    def _file_index_helper(root_dir: PathType, file_pattern: str) -> List[Tuple[str, int]]:
        """Build the file index in a multiprocessing context.

        Args:
            root_dir: Root directory.
            file_pattern: File pattern string.

        Returns:
            The list of keys within the glob context.
        """
        prevent_fsspec_deadlock()
        return [(key.parts[-4], int(key.stem)) for key in root_dir.glob(file_pattern)]

    def _read_frame(self, src_path: PathType, file_caching_path: PathType) -> pl.DataFrame:
        """Read a dataframe from a remote source or a locally cached location.

        Args:
            src_path: Path to the non-cached file.
            file_caching_path: Path to the cached file.

        Returns:
            DataFrame representation of the feather file.
        """
        if self.file_caching_mode == FileCachingMode.DISK:
            file_caching_path.parent.mkdir(parents=True, exist_ok=True)
            lock_name = str(file_caching_path) + ".lock"
            with FileLock(lock_name):
                if not file_caching_path.exists():
                    dataframe = read_feather(src_path)
                    dataframe.write_ipc(file_caching_path)
                else:
                    try:
                        dataframe = read_feather(file_caching_path)
                    except Exception as _:
                        dataframe = read_feather(src_path)
                        dataframe.write_ipc(file_caching_path)
        else:
            dataframe = read_feather(src_path)
        return dataframe

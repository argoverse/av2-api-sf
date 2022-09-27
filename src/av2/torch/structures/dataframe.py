"""Backend agnostic DataFrame abstraction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import List, Tuple, Union

import pandas as pd
import polars as pl
from pyarrow import feather

from av2.utils.typing import NDArrayBool, NDArrayNumber, PathType


@unique
class DataFrameBackendType(str, Enum):
    """DataFrame compute backends."""

    PANDAS = "PANDAS"
    POLARS = "POLARS"


@dataclass
class DataFrame:
    """Backend agnostic dataframe."""

    storage: Union[pd.DataFrame, pl.DataFrame]
    backend: DataFrameBackendType = DataFrameBackendType.PANDAS

    def __getitem__(self, index: Union[List[str], pd.DataFrame]) -> DataFrame:
        """Get the item at the specified indices.

        Args:
            index: Mask or index.

        Returns:
            DataFrame with the selected elements.
        """
        if self.backend == DataFrameBackendType.POLARS:
            dataframe_polars: pl.DataFrame = self.storage.select(pl.col(index))
            return DataFrame(dataframe_polars, backend=self.backend)
        if isinstance(index, (List, str)):
            return DataFrame(self.storage[index], backend=self.backend)

        # TODO: Better communicate types.
        index_npy: NDArrayBool = index.to_numpy()
        return DataFrame(self.storage[index_npy], backend=self.backend)

    @property
    def shape(self) -> Tuple[int, int]:
        """Return the shape of the DataFrame."""
        return self.storage.shape

    @property
    def columns(self) -> List[str]:
        """Return the columns of the DataFrame."""
        return list(self.storage.columns)

    @classmethod
    def read(cls, path: PathType, backend: DataFrameBackendType = DataFrameBackendType.PANDAS) -> DataFrame:
        """Read a feather file into a DataFrame.

        Args:
            path: Source feather file path.
            backend: DataFrame backend for initialization.

        Returns:
            The DataFrame.
        """
        with path.open("rb") as file_handle:
            if backend == DataFrameBackendType.PANDAS:
                dataframe_pandas: pd.DataFrame = feather.read_feather(file_handle, memory_map=True)
                return cls(dataframe_pandas, backend=backend)
            if backend == DataFrameBackendType.POLARS:
                dataframe_polars = pl.read_ipc(file_handle, memory_map=True)
                return cls(dataframe_polars, backend=backend)
            raise NotImplementedError("This backend is not implemented!")

    def write(self, path: PathType) -> None:
        """Write the DataFrame to a feather file.

        Args:
            path: Feather file destination path.
        """
        with path.open("wb") as file_handle:
            if self.backend == DataFrameBackendType.PANDAS:
                feather.write_feather(self.storage, file_handle, compression=None)
            if self.backend == DataFrameBackendType.POLARS:
                self.storage.write_ipc(file_handle)
            raise NotImplementedError("This backend is not implemented!")

    @classmethod
    def concat(cls, dataframe_list: List["DataFrame"], axis: int = 0) -> DataFrame:
        """Concatenate a list of DataFrames into one DataFrame.

        Args:
            dataframe_list: List of dataframes.
            axis: Axis of concatenation.

        Returns:
            The DataFrame.
        """
        dataframe_backend_type = dataframe_list[0].backend
        if dataframe_backend_type == DataFrameBackendType.PANDAS:
            _dataframe_pandas_list: List[pd.DataFrame] = [dataframe.storage for dataframe in dataframe_list]
            dataframe_pandas: pd.DataFrame = pd.concat(_dataframe_pandas_list, axis=axis).reset_index(drop=True)
            return cls(dataframe_pandas, backend=dataframe_backend_type)
        if dataframe_backend_type == DataFrameBackendType.POLARS:
            _dataframe_polars_list: List[pl.DataFrame] = [dataframe.storage for dataframe in dataframe_list]
            dataframe_polars = pl.concat(_dataframe_polars_list, how="vertical" if axis == 0 else "horizontal")
            return cls(dataframe_polars, backend=dataframe_backend_type)
        raise NotImplementedError("This backend is not implemented!")

    @classmethod
    def from_numpy(
        cls, arr: NDArrayNumber, columns: List[str], backend: DataFrameBackendType = DataFrameBackendType.PANDAS
    ) -> DataFrame:
        """Convert the numpy ndarray into a DataFrame.

        Args:
            arr: Numpy array.
            columns: List of column names.
            backend: DataFrame backend for initialization.

        Returns:
            The DataFrame.
        """
        if backend == DataFrameBackendType.PANDAS:
            dataframe_pandas = pd.DataFrame(arr, columns=columns)
            return cls(dataframe_pandas)
        if backend == DataFrameBackendType.POLARS:
            dataframe_polars = pl.DataFrame(arr, columns=columns)
            return cls(dataframe_polars)
        raise NotImplementedError("This backend is not implemented!")

    def to_numpy(self) -> NDArrayNumber:
        """Convert the DataFrame into a numpy ndarray."""
        arr: NDArrayNumber = self.storage.to_numpy()
        return arr

    def __gt__(self, x) -> DataFrame:
        return DataFrame(self.storage > x, backend=self.backend)

    def __le__(self, x) -> DataFrame:
        return DataFrame(self.storage < x, backend=self.backend)

    def __eq__(self, x) -> DataFrame:
        return DataFrame(self.storage == x, backend=self.backend)

    def __and__(self, x) -> DataFrame:
        return DataFrame(self.storage & x.storage, backend=self.backend)

    def sort(self, columns: List[str]) -> DataFrame:
        """Sort the DataFrame with respect to the columns.

        Args:
            columns: List of column names.

        Returns:
            The sorted DataFrame.
        """
        if self.backend == DataFrameBackendType.PANDAS:
            dataframe_pandas: pd.DataFrame = self.storage.sort_values(columns)
            return DataFrame(dataframe_pandas)
        if self.backend == DataFrameBackendType.POLARS:
            dataframe_polars: pl.DataFrame = self.storage.sort(columns)
            return DataFrame(dataframe_polars)
        raise NotImplementedError("This backend is not implemented!")

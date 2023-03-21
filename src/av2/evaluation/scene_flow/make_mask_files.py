"""Utility program for producing submission mask files."""

import zipfile
from pathlib import Path
from typing.IO import BinaryIO

import click
import pandas as pd
from kornia.geometry.liegroup import Se3
from rich.progress import track

from av2.evaluation.scene_flow.utils import compute_eval_point_mask, get_eval_subset
from av2.torch.data_loaders.scene_flow import SceneFlowDataloader
from av2.torch.structures.sweep import Sweep


def get_mask(
    s0: Sweep,
    s1: Sweep,
    s1_SE3_s0: Se3,
) -> pd.DataFrame:
    """Get a mask packaged up into a DataFrame.

    Args:
        s0: The first sweep of the pair.
        s1: The second sweep of the pair.
        s1_SE3_s0: The relative ego-motion between the two sweeps.

    Returns:
        DataFrame with a single column for the mask.
    """
    mask = compute_eval_point_mask((s0, s1, s1_SE3_s0, None))
    output = pd.DataFrame({"mask": mask.numpy().astype(bool)})
    return output


@click.command()
@click.argument("output_dir", type=str)
@click.argument("data_dir", type=str)
@click.option(
    "--name",
    type=str,
    help="the data should be located in <data_dir>/<name>/sensor/<split>",
    default="av2",
)
@click.option(
    "--split",
    help="the data should be located in <data_dir>/<name>/sensor/<split>",
    default="val",
    type=click.Choice(["test", "val"]),
)
def make_mask_files(output_file: str, data_dir: str, name: str, split: str) -> None:
    """Output the point masks for submission to the leaderboard.

    Args:
        output_file: Path to output files.
        data_dir: Path to input data.
        name: Name of the dataset (e.g. av2).
        split: Split to make masks for.
    """
    data_loader = SceneFlowDataloader(Path(data_dir), name, split)

    output_root = Path(output_dir)
    output_root.mkdir(exist_ok=True)

    eval_inds = get_eval_subset(data_loader)

    with ZipFile(Path(output_file), "w") as maskzip:
        for i in track(eval_inds):
            sweep_0, sweep_1, ego, _ = data_loader[i]
            write_mask(sweep_0, sweep_1, ego, output_root)


if __name__ == "__main__":
    make_mask_files()

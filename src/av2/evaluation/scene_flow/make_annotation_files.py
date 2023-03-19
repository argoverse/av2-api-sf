"""Utility program for producing minimnal annotation files used for evaluation on the val and test splits."""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from rich.progress import track

from av2.evaluation.scene_flow.utils import get_eval_point_mask, get_eval_subset
from av2.torch.dataloaders.scene_flow import SceneFlowDataloader

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        "make_annotation_files",
        description="Make a directory of feather files storing " "just enough info to run a scene flow evaluation",
    )
    parser.add_argument("output_root", type=str, help="path/to/output/")
    parser.add_argument("data_root", type=str, help="root/path/to/data")
    parser.add_argument(
        "--name", type=str, default="av2", help="the data should be located in <data_root>/<name>/sensor/<split>"
    )
    parser.add_argument(
        "--split",
        type=str,
        default="val",
        choices=["val", "test"],
        help="the data should be located in <data_root>/<name>/sensor/<split>",
    )

    args = parser.parse_args()

    dl = SceneFlowDataloader(args.data_root, args.name, args.split)

    output_root = Path(args.output_root)
    output_root.mkdir(exist_ok=True)

    eval_inds = get_eval_subset(dl)
    for i in track(eval_inds):
        datum = dl[i]
        if datum[3] is None:
            raise ValueError("Missing flow annotations")

        mask = get_eval_point_mask(datum)

        flow = datum[3].flow[mask].numpy().astype(np.float16)
        valid = datum[3].valid[mask].numpy().astype(bool)
        classes = datum[3].classes[mask].numpy().astype(np.uint8)
        dynamic = datum[3].dynamic[mask].numpy().astype(bool)

        pc = datum[0].lidar_xyzi[mask, :3].numpy()
        close = ((np.abs(pc[:, 0]) <= 35) & (np.abs(pc[:, 1]) <= 35)).astype(bool)

        output = pd.DataFrame(
            {
                "classes": classes,
                "close": close,
                "dynamic": dynamic,
                "valid": valid,
                "flow_tx_m": flow[:, 0],
                "flow_ty_m": flow[:, 1],
                "flow_tz_m": flow[:, 2],
            }
        )

        log, ts = datum[0].sweep_uuid

        output_dir = output_root / log
        output_dir.mkdir(exist_ok=True)
        output_file = (output_dir / str(ts)).with_suffix(".feather")
        output.to_feather(output_file)
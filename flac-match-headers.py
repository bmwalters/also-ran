#!/usr/bin/env python3

import argparse
from functools import partial
from io import BytesIO
import multiprocessing
from mutagen.flac import FLAC, Padding, SeekTable, StreamInfo
from os import cpu_count
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Callable, Optional

class AudioDiffersError(Exception):
    pass

class EncodingDiffersError(Exception):
    pass

def get_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Make metadata of flac files match given flac header files."
    )
    parser.add_argument(
        "--headers", type=Path, required=True,
        help="directory containing flac headers"
    )
    parser.add_argument(
        "--in", dest="input", type=Path, required=True,
        help="directory containing flacs which should be updated"
    )
    parser.add_argument(
        "--out", type=Path, required=True,
        help="directory to write changed flacs to"
    )
    parser.add_argument(
        "--flac", type=Path,
        help="path to flac executable to encode with if necessary"
    )
    parser.add_argument(
        "--flac-args", type=str, help="arguments to flac encoder if necessary"
    )
    # TODO: think about last track differences between pressings
    parser.add_argument(
        "--skip-track", type=int, action="append", default=[],
        help="tracks to skip matching"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=cpu_count(),
        help="number of parallel jobs (default: num cpus))"
    )
    parser.add_argument(
        "-q", "--quiet", type=bool, default=False, help="quiet output",
        action=argparse.BooleanOptionalAction
    )
    return parser

def _check_audio_data_match(input: FLAC, header: FLAC) -> None:
    input_streaminfo = input.metadata_blocks[0]
    header_streaminfo = header.metadata_blocks[0]
    if input_streaminfo.md5_signature != header_streaminfo.md5_signature:
        raise AudioDiffersError()

def _check_encoding_match(input: FLAC, header: FLAC) -> None:
    if input.tags.vendor != header.tags.vendor:
        raise EncodingDiffersError()

    input_streaminfo = input.metadata_blocks[0]
    header_streaminfo = header.metadata_blocks[0]
    attrs = ["min_blocksize", "max_blocksize", "min_framesize", "max_framesize"]
    for attr in attrs:
        if getattr(input_streaminfo, attr) != getattr(header_streaminfo, attr):
            raise EncodingDiffersError()

    header_seektable = list(filter(
        lambda b: isinstance(b, SeekTable),
        header.metadata_blocks
    ))
    assert len(header_seektable) <= 1, \
        "Not smart enough to match headers with multiple seek table blocks."
    input_seektable = next(
        block for block in input.metadata_blocks if isinstance(block, SeekTable)
    )
    if input_seektable != header_seektable[0]:
        raise EncodingDiffersError()

def _get_flac_padding(flac: FLAC) -> int:
    padding_blocks = list(filter(
        lambda b: isinstance(b, Padding),
        flac.metadata_blocks
    ))
    assert len(padding_blocks) == 1, \
        "Not smart enough to match headers with multiple padding blocks."
    return padding_blocks[0].length

def _match_flac(
        flac: FLAC, flac_path: Path, header: FLAC, out_path: Path,
        re_encode: Callable[[Path, FLAC], FLAC]
) -> None:
    header_block_order = list(map(type, header.metadata_blocks))
    input_block_order = list(map(type, flac.metadata_blocks))
    assert input_block_order == header_block_order, \
        f"Not smart enough to reorder metadata blocks " + \
        f"({input_block_order} != {header_block_order})"
    assert header_block_order[0] == StreamInfo, \
        "The first metadata block must be stream info."

    _check_audio_data_match(flac, header)

    need_reencode = False
    try:
        _check_encoding_match(flac, header)
    except EncodingDiffersError as e:
        need_reencode = True

    if need_reencode:
        flac = re_encode(flac_path, header, out_path)
        flac_path = out_path
        _check_encoding_match(flac, header)

    flac.tags.clear()
    for k, v in header.tags:
        flac.tags.append((k, v))
    out_padding = lambda info: _get_flac_padding(header)

    # mutagen only rewrites metadata
    # https://github.com/quodlibet/mutagen/issues/493
    out_bio = BytesIO(flac_path.read_bytes())
    flac.save(out_bio, padding=out_padding)
    out_path.write_bytes(out_bio.getbuffer())


def _get_flac_executable(
        user_provided: Optional[Path], flac_version: str
) -> str:
    if user_provided is not None:
        have_version = subprocess.check_output(
            [user_provided, "--version"], encoding="utf-8"
        )
        assert have_version == f"flac {flac_version}\n", \
            f"Given `flac` version {have_version} but need {flac_version}."
        return user_provided

    named = f"flac-{flac_version}"
    from_path = named if shutil.which(named) is not None else "flac"
    have_version = subprocess.check_output(
        [from_path, "--version"], encoding="utf-8"
    )
    assert have_version == f"flac {flac_version}\n", \
        f"`{from_path}` is version {have_version} but need {flac_version}. " + \
        "Provide the --flac argument with the path to the right executable."
    return from_path

def _flac_version_from_vendor_string(vendor: str) -> str:
    flac_version = re.match(r"reference libFLAC ([\d\.]+?) ", vendor)
    assert flac_version is not None, f"unrecognized vendor {vendor}"
    return flac_version[1]

def _re_encode(
        flac_executable: Optional[Path], flac_args: Optional[str], quiet: bool,
        input_path: Path, header: FLAC, out_path: Path
) -> FLAC:
    flac_version = _flac_version_from_vendor_string(header.tags.vendor)
    flac_executable = _get_flac_executable(flac_executable, flac_version)

    # TODO: automatically obtain from eac log if present in headers dir
    assert flac_args is not None, "A re-encode is required. " + \
        "Provide --flac-args to match the encode."
    flac_args = shlex.split(flac_args)

    subprocess.run(
        [flac_executable, *flac_args, input_path, "-fo", out_path], check=True,
        **({"stderr": subprocess.DEVNULL} if quiet else {})
    )

    return FLAC(out_path)

def main():
    args = get_argument_parser().parse_args()

    re_encode = partial(_re_encode, args.flac, args.flac_args, args.quiet)

    # map track numbers to files in args.input; keep things linear
    trackno_to_input = {}
    for input_path in args.input.glob("*.flac"):
        input = FLAC(input_path)
        trackno = int(input.tags["tracknumber"][0])
        assert trackno not in trackno_to_input
        trackno_to_input[trackno] = (input, input_path)

    # core loop: for each header, make an input file match, then save to out dir
    with multiprocessing.Pool(args.jobs) as pool:
        tasks = []
        for header_path in args.headers.glob("*.flac.part"):
            header = FLAC(header_path)
            trackno = int(header.tags["tracknumber"][0])

            if trackno in args.skip_track:
                continue

            input, input_path = trackno_to_input[trackno]
            out_path = Path(args.out, header_path.name.replace(".part", ""))

            tasks.append((input, input_path, header, out_path, re_encode))

        try:
            pool.starmap(_match_flac, tasks)
        except AudioDiffersError as e:
            assert False, \
                f"Audio data in {input_path} differs from {header_path}: {e}"

if __name__ == "__main__":
    main()

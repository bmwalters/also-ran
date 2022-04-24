#!/usr/bin/env python3

import argparse
from functools import partial
import io
import multiprocessing
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
import re
from os import cpu_count
from pathlib import Path
import shutil
import subprocess

def get_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcode a directory of FLACs to MP3."
    )
    parser.add_argument(
        "-i", "--in", dest="in_path", type=Path, required=True,
        help="input directory"
    )
    parser.add_argument(
        "-o", "--out", type=Path, help="output directory (default: automatic)"
    )
    parser.add_argument(
        "--preset", choices=["320", "V0"], required=True,
        help="bitrate (320kbps CBR or V0 ABR)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=cpu_count(),
        help="number of parallel jobs (default: num cpus))"
    )
    parser.add_argument(
        "--transcode", type=bool, default=True,
        action=argparse.BooleanOptionalAction,
        help="on to transcode + copy other files, off to copy only"
    )
    return parser

def make_output_path(in_path: Path, preset: str) -> Path:
    assert len(in_path.name.split("FLAC")) == 2, \
        "automatic output directory naming requires input to contain 'FLAC' once"
    return in_path.parent / in_path.name.replace("FLAC", preset)

def transcode_flac_to_mp3(
        preset: str, out_dir_path: Path, in_path: Path
) -> Path:
    out_path = out_dir_path / in_path.with_suffix(".mp3").name

    flac_sp = subprocess.Popen(
        ["flac", "-d", "-c", in_path],
        stdout=subprocess.PIPE
    )

    # we need to use the identifiable lame encoder
    encoder_opts = ["-V", "0"] if preset == "V0" else ["-b", "320"]
    subprocess.run(
        ["lame", *encoder_opts, "--add-id3v2", "-", out_path],
        stdin=flac_sp.stdout, check=True
    )

    flac = FLAC(in_path)
    mp3 = EasyID3(out_path)

    tracktotal = None
    disctotal = None

    for tag in flac:
        if tag == "totaltracks" or tag == "tracktotal":
            assert tracktotal is None and len(flac[tag]) == 1, \
                f"found multiple tracktotal in {in_path}"
            tracktotal = flac[tag][0]
        elif tag == "disctotal" or tag == "totaldiscs":
            assert disctotal is None and len(flac[tag]) == 1, \
                f"found multiple disctotal in {in_path}"
            disctotal = flac[tag][0]
        else:
            assert tag in EasyID3.valid_keys.keys(), \
                f"unknown metadata tag {tag} in {in_path}"
            mp3[tag] = flac[tag]

    if tracktotal is not None:
        tracknumber = mp3["tracknumber"]
        assert len(tracknumber) == 1 and tracknumber[0].isdigit(), \
            f"found tracktotal & non-int tracknumber {tracknumber} in {in_path}"
        mp3["tracknumber"] = f"{tracknumber[0]}/{tracktotal}"
    if disctotal is not None:
        discnumber = mp3["discnumber"]
        assert len(discnumber) == 1 and discnumber[0].isdigit(), \
            f"found disctotal & non-int discnumber {discnumber} in {in_path}"
        mp3["discnumber"] = f"{discnumber[0]}/{disctotal}"

    mp3.save(None, v1=0, v2_version=3)

    return out_path

# TODO: Real m3u8/cue parsing; ensure filenames exist as specified.

def fixup_cue(infile: str, outfile: str) -> None:
    with io.open(outfile, "w", encoding="iso-8859-1") as outfile:
        with io.open(infile, "r", encoding="iso-8859-1", newline="") as infile:
            for line in infile:
                if line.startswith("FILE "):
                    assert ".wav" in line
                    assert "WAVE" in line
                    outfile.write(
                        line.replace(".wav", ".mp3").replace("WAVE", "MP3")
                    )
                else:
                    outfile.write(line)

def fixup_m3u8(infile: str, outfile: str) -> None:
    with io.open(outfile, "w") as outfile:
        with io.open(infile, "r", newline="") as infile:
            for line in infile:
                outfile.write(
                    re.sub(pattern=r"\.flac(\s*)$", repl=".mp3\\1", string=line)
                )

def main():
    EasyID3.RegisterTextKey("tracktotal", "TRCK")
    EasyID3.RegisterTextKey("totaltracks", "TRCK")

    args = get_argument_parser().parse_args()

    assert args.in_path.is_dir(), \
        "input path must be a directory containing flac files"

    out_path = args.out or make_output_path(args.in_path, args.preset)
    out_path.mkdir(parents=True, exist_ok=True)

    if args.transcode:
        with multiprocessing.Pool(args.jobs) as pool:
            flac_paths = list(args.in_path.glob("*.flac"))
            mp3_paths = list(pool.imap(
                partial(transcode_flac_to_mp3, args.preset, out_path),
                flac_paths
            ))

    for extra_in_path in args.in_path.iterdir():
        extra_out_path = out_path / extra_in_path.name

        if extra_in_path.suffix == ".flac":
            continue
        elif extra_in_path.suffix == ".cue":
            fixup_cue(extra_in_path, extra_out_path)
        elif extra_in_path.suffix == ".m3u8":
            fixup_m3u8(extra_in_path, extra_out_path)
        else:
            assert extra_in_path.suffix in {".jpg", ".jpeg", ".png", ".log"}, \
                f"unknown extra file {extra_in_path} found in input path"
            shutil.copy(extra_in_path, extra_out_path)

if __name__ == "__main__":
    main()

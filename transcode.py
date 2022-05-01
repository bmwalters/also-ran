#!/usr/bin/env python3

import argparse
from functools import partial
import io
import multiprocessing
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
import mutagen.id3
import re
from os import cpu_count
from pathlib import Path
import shutil
import subprocess
from typing import Optional

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
    parser.add_argument(
        "--lineage", type=str,
        help="Lineage information to include in ID3 comment."
    )
    parser.add_argument(
        "-q", "--quiet", type=bool, default=False, help="quiet output",
        action=argparse.BooleanOptionalAction
    )
    return parser

def make_output_path(in_path: Path, preset: str) -> Path:
    assert len(in_path.name.split("FLAC")) == 2, \
        "automatic output directory naming requires input to contain 'FLAC' once"
    return in_path.parent / in_path.name.replace("FLAC", preset)

def transcode_flac_to_mp3(
        preset: str, lineage: Optional[str], quiet: bool,
        out_dir_path: Path, in_path: Path
) -> Path:
    out_path = out_dir_path / in_path.with_suffix(".mp3").name

    flac_version = subprocess.check_output(
        ["flac", "--version"], encoding="utf-8"
    ).strip()
    flac_sp = subprocess.Popen(
        ["flac", "-d", "-c", in_path], stdout=subprocess.PIPE,
        **({"stderr": subprocess.DEVNULL} if quiet else {})
    )

    # we need to use the identifiable lame encoder
    encoder_opts = ["-V", "0"] if preset == "V0" else ["-b", "320"]
    subprocess.run(
        ["lame", *encoder_opts, "--add-id3v2", "-", out_path],
        stdin=flac_sp.stdout, check=True,
        **({"stderr": subprocess.DEVNULL} if quiet else {})
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
        elif tag == "encoder":
            continue
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

    # combine comment fields as multi-valued tags are tricky
    # (variable separator character, lack of ffprobe support for \0 separator)
    existing_comments = [*mp3["comment"], ""] \
        if "comment" in mp3.keys() and mp3["comment"] else []
    new_comment = "\n".join((s for s in [
        *existing_comments,
        lineage,
        f"Decoded with {flac_version}. Encoded with 'lame {' '.join(encoder_opts)}'.",
        "Tags mapped using EasyID3 from https://github.com/quodlibet/mutagen."
    ] if s is not None))
    mp3["comment"] = new_comment

    if len(flac.pictures) > 0:
        assert len(flac.pictures) == 1, "not smart enough to handle >1 pictures"
        mp3._EasyID3__id3.add(
            mutagen.id3.APIC(
                encoding=mutagen.id3.Encoding.LATIN1,
                mime=flac.pictures[0].mime,
                type=flac.pictures[0].type,
                desc=flac.pictures[0].desc,
                data=flac.pictures[0].data
            )
        )

    mp3.save(None, v1=0, v2_version=3)

    return out_path

# TODO: Real m3u8/cue parsing; ensure filenames exist as specified.

def fixup_cue(infile: str, outfile: str) -> None:
    with io.open(outfile, "w", encoding="iso-8859-1") as outfile:
        with io.open(infile, "r", encoding="iso-8859-1", newline="") as infile:
            for line in infile:
                if line.startswith("FILE "):
                    assert ".wav" in line or ".flac" in line
                    assert "WAVE" in line
                    outfile.write(
                        line.replace(".wav", ".mp3")
                            .replace(".flac", ".mp3")
                            .replace("WAVE", "MP3")
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

# https://github.com/TravisCardwell/mutagen/commit/965724eae83b7cd7bd4ad20c9bb3bf7fe0bc9626
def EasyID3_RegisterCommentKey(key: str, lang="\0\0\0", desc=""):
    """Register a comment key, stored in a COMM frame.
    By default, comments use a null language and empty description, for
    compatibility with other tagging software.  Call this method with
    other parameters to override the defaults.::
        EasyID3.RegisterCOMMKey(lang='eng')
    """
    frameid = ":".join(("COMM", desc, lang))

    def getter(id3, key):
        frame = id3.get(frameid)
        return None if frame is None else list(frame)

    def setter(id3, key, value):
        id3.add(mutagen.id3.COMM(encoding=3, lang=lang, desc=desc, text=value))

    def deleter(id3, key):
        del id3[frameid]

    EasyID3.RegisterKey(key, getter, setter, deleter)

def main():
    easyid3_valid_keys = EasyID3.valid_keys.keys()
    if "tracktotal" not in easyid3_valid_keys:
        EasyID3.RegisterTextKey("tracktotal", "TRCK")
        EasyID3.RegisterTextKey("totaltracks", "TRCK")
    if "album artist" not in easyid3_valid_keys:
        EasyID3.RegisterTextKey("album artist", "TPE2")
    if "label" not in easyid3_valid_keys:
        EasyID3.RegisterTextKey("label", "TPUB")
    if "itunescompilation" not in easyid3_valid_keys:
        EasyID3.RegisterTextKey("itunescompilation", "TCMP")
    if "description" not in easyid3_valid_keys:
        EasyID3_RegisterCommentKey("description")
    if "comment" not in easyid3_valid_keys:
        EasyID3_RegisterCommentKey("comment")
    if "itunes_cddb_1" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("itunes_cddb_1", "ITUNES_CDDB_1")
    if "labelno" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("labelno", "CATALOGNUMBER")
    if "mcn" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("mcn", "MCN")
    if "upc" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("upc", "UPC")
    if "release country" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("release country", "MusicBrainz Album Release Country")
    if "replaygain_track_peak" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("replaygain_track_peak", "REPLAYGAIN_TRACK_PEAK")
    if "replaygain_track_gain" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("replaygain_track_gain", "REPLAYGAIN_TRACK_GAIN")
    # bandcamp
    if "cataloguenumber" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("cataloguenumber", "CATALOGNUMBER")
    if "publisher" not in easyid3_valid_keys:
        EasyID3.RegisterTXXXKey("publisher", "PUBLISHER")

    args = get_argument_parser().parse_args()

    assert args.in_path.is_dir(), \
        "input path must be a directory containing flac files"

    out_path = args.out or make_output_path(args.in_path, args.preset)
    out_path.mkdir(parents=True, exist_ok=True)

    if args.transcode:
        with multiprocessing.Pool(args.jobs) as pool:
            flac_paths = list(args.in_path.glob("*.flac"))
            mp3_paths = list(pool.imap(
                partial(
                    transcode_flac_to_mp3,
                    args.preset, args.lineage, args.quiet, out_path
                ),
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

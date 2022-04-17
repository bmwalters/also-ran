#!/usr/bin/env python3

from argparse import ArgumentParser
from io import BytesIO
from pathlib import Path
import re

from mutagen.flac import FLAC, Padding

def get_argument_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Make flac metadata match a folder of headers.")
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
    return parser

def _get_flac_padding(flac: FLAC) -> int:
    padding_blocks = list(filter(
        lambda b: isinstance(b, Padding),
        flac.metadata_blocks
    ))
    assert len(padding_blocks) == 1, \
        "Not smart enough to match headers with multiple padding blocks."
    return padding_blocks[0].length

def main():
    args = get_argument_parser().parse_args()

    # map track numbers to files in args.input; keep things linear
    trackno_to_input = {}
    for input_path in args.input.glob("*.flac"):
        input = FLAC(input_path)
        trackno = int(input.tags["tracknumber"][0])
        assert trackno not in trackno_to_input
        trackno_to_input[trackno] = (input, input_path)

    # core loop: for each header, make an input file match, then save to out dir
    for header_path in args.headers.glob("*.flac.part"):
        print(header_path)
        header = FLAC(header_path)
        trackno = int(header.tags["tracknumber"][0])
        input, input_path = trackno_to_input[trackno]

        # throw assertions if audio data itself does not match
        assert input.info.pprint() == header.info.pprint(), \
            f"FLAC diff! (in) {input.info.pprint()} != {header.info.pprint()}"
        assert input.tags.vendor == header.tags.vendor, \
            f"FLAC vendor diff! (in) {input.tags.vendor} != {header.tags.vendor}"

        input.tags = header.tags
        out_padding = lambda info: _get_flac_padding(header)

        out_path = Path(args.out, header_path.name.replace(".part", ""))

        # https://github.com/quodlibet/mutagen/issues/493
        out_bio = BytesIO(input_path.read_bytes())
        input.save(out_bio, padding=out_padding)
        out_path.write_bytes(out_bio.getbuffer())

if __name__ == "__main__":
    main()

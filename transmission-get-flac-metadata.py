#!/usr/bin/env python3

import argparse
import os.path
from sys import exit
from time import sleep
from traceback import print_exception

from mutagen.flac import FLAC
from transmission_rpc import Client, File, Session, Torrent

DEFAULT_TIMEOUT = 10

# must be set to include all fields needed by all methods.
GET_TORRENT_ARGS = (
    "id", "files", "fileStats", "priorities", "wanted",
    "downloadDir", "downloadLimit", "downloadLimited"
)

def get_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="""Retrieve metadata from flac files within a torrent.

Attempts to control `transmission` to download FLAC metadata without
downloading entire files within torrents.

Given torrent id(s), downloads ~500 KB of each FLAC file in each torrent.
Next, sanity checks & raises error if headers were not downloaded.
"""
    )
    parser.add_argument(
        "-t", "--torrent", type=int, required=True,
        help="torrent id selector; see transmission-remote(1)"
    )
    parser.add_argument(
        "--timeout", type=int, help="seconds to wait for rpc replies",
        default=DEFAULT_TIMEOUT
    )
    parser.add_argument(
        "--progress", type=bool, help="report progress", default=False,
        action=argparse.BooleanOptionalAction
    )
    parser.add_argument("--rpc-protocol", type=str, default="http")
    parser.add_argument("--rpc-username", type=str)
    parser.add_argument("--rpc-password", type=str)
    parser.add_argument("--rpc-host", type=str, default="127.0.0.1")
    parser.add_argument("--rpc-port", type=int, default=9091)
    parser.add_argument("--rpc-path", type=str, default="/transmission/")
    return parser

def _get_file_path(session: Session, torrent: Torrent, file: File):
    path = os.path.join(torrent.download_dir, file.name)
    if session.rename_partial_files and file.completed != file.size:
        path += ".part"
    return path

def download_all_flac_headers(
        client: Client,
        torrent: Torrent,
        report_progress: bool = False
) -> Torrent:
    torrent_files = torrent.files()

    original_download_limit = torrent.download_limit
    original_files_unwanted = list(filter(
        lambda i_f: not i_f[1].selected,
        enumerate(torrent_files)
    ))
    original_files_wanted = list(filter(
        lambda i_f: i_f[1].selected,
        enumerate(torrent_files)
    ))

    try:
        latest_torrent = torrent
        for file_id, file in enumerate(torrent_files):
            if not file.name.endswith(".flac"):
                continue

            # set download limit & only download this file id
            wanted = [file_id]
            unwanted = [id for id in range(len(torrent_files)) if id != file_id]
            client.change_torrent(
                ids=[torrent.id],
                downloadLimit=50, downloadLimited=True,
                files_unwanted=unwanted, files_wanted=wanted
            )

            # start the torrent until 1 MB is downloaded
            client.start_torrent(ids=[torrent.id])

            try:
                latest_file_info = file
                while latest_file_info.completed < 500_000:
                    latest_torrent = client.get_torrent(
                        torrent_id=torrent.id, arguments=GET_TORRENT_ARGS,
                    )
                    latest_file_info = latest_torrent.files()[file_id]
                    sleep(0.5)
                if report_progress:
                    print(f"torrent {torrent.id} downloaded 500 KB of file {file_id}")
            except KeyboardInterrupt:
                print("Received KeyboardInterrupt, trying next file...")
                continue

        return latest_torrent
    finally:
        download_limit_args = {
            "downloadLimit": original_download_limit,
            "downloadLimited": True,
        } if original_download_limit is not None else {
            "downloadLimited": False,
        }
        client.change_torrent(
            ids=[torrent.id],
            files_unwanted=original_files_unwanted,
            files_wanted=original_files_wanted,
            **download_limit_args
        )
        client.stop_torrent(ids=[torrent.id])

def check_flac_headers(session: Session, torrent: Torrent):
    for file in torrent.files():
        if not file.name.endswith(".flac"):
            continue
        audio = FLAC(_get_file_path(session, torrent, file))
        assert audio.tags["title"] is not None

def main():
    args = get_argument_parser().parse_args()

    # We use transmission to obey tracker allowlists.
    # Maybe look into other strategies which use a Python torrent client.
    client = Client(
        protocol=args.rpc_protocol, host=args.rpc_host, port=args.rpc_port,
        username=args.rpc_username, password=args.rpc_password,
        path=args.rpc_path, timeout=args.timeout
    )

    # core loop: for each torrent, download flac file headers.
    # This could probably be parallelized, but that complicates error handling.
    torrents = client.get_torrents(
        ids=args.torrent, arguments=GET_TORRENT_ARGS
    )
    results = []

    for torrent in torrents:
        # try the actual download
        error = None
        try:
            torrent = download_all_flac_headers(
                client, torrent=torrent, report_progress=args.progress
            )
        except Exception as download_error:
            error = download_error
        finally:
            client.stop_torrent(ids=[torrent.id])

        # wait for io (untested)
        sleep(2)

        # check if we obtained valid headers
        if error is None:
            try:
                check_flac_headers(session=client.session, torrent=torrent)
            except Exception as check_error:
                error = check_error

        # save results for this torrent
        results.append((torrent.id, error))
        if args.progress:
            print(f"torrent {torrent.id} result {error or 'good'}")

    # report results
    errored = False
    print("=== summary ===")
    for torrent_id, result in results:
        if result:
            errored = True
            print(f"x {torrent_id} {repr(result)}")
            print_exception(result)
        else:
            print(f"o {torrent_id} good")

    if errored:
        exit(1)

if __name__ == "__main__":
    main()

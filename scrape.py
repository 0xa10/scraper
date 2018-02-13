#!/usr/local/bin/python3
"""Asynchronous scraping code"""
import asyncio
import cgi
from contextlib import contextmanager
import functools
import logging
import os
import pathlib

import blessings
from bs4 import BeautifulSoup
import coloredlogs
import progressbar

import aiofiles
import aiohttp

BANNER = "\n".join((r" ____                                 ",
                    r"/ ___|  ___ _ __ __ _ _ __   ___ _ __ ",
                    r"\___ \ / __| '__/ _` | '_ \ / _ \ '__|",
                    r" ___) | (__| | | (_| | |_) |  __/ |   ",
                    r"|____/ \___|_|  \__,_| .__/ \___|_|   ",
                    r"                     |_|              "))


MAIN_BAR_WIDGETS = ['Tasks complete: ',
                    progressbar.SimpleProgress(),
                    ' (', progressbar.Percentage(), ') ',
                    progressbar.AnimatedMarker()]
SUB_BAR_WIDGETS = [': ',
                   ' (', progressbar.Percentage(), ') ',
                   progressbar.Bar(), progressbar.FileTransferSpeed()]


class ProgressBarManager(object):
    """Draws a main progress bars and sub-task progress bars on a given blessings terminal"""
    SUB_LINE_OFFSET = 1
    SUB_INDENT = 4
    DEFAULT_COORDS = (0, 0)
    DEFAULT_LINE_MAX = 10

    def __init__(self, terminal, main_coords=None, lines=None):
        """Takes a terminal object, anchor location and amount of lines for sub bars"""
        self._terminal = terminal
        self._main_coords = main_coords if main_coords else self.DEFAULT_COORDS
        self._sub_coords = (main_coords[0] + self.SUB_INDENT, main_coords[1] + self.SUB_LINE_OFFSET)
        self._lines = [None,] * (lines if lines else self.DEFAULT_LINE_MAX)

    def _get_sub_line(self, idx):
        return (self._sub_coords[0], self._sub_coords[1] + idx)

    def _alloc_slot(self):
        if None in self._lines:
            return self._lines.index(None)
        return None

    def _free_slot(self, slot):
        self._lines[slot] = None

    @contextmanager
    def install_main(self, progress_bar):
        """Context manager for starting and positioning main progress bar"""
        # Replace writer
        progress_bar.fd = Writer(self._main_coords)
        # Make bold
        progress_bar.widgets = [TERM.bold,] + progress_bar.widgets
        progress_bar.start()
        yield progress_bar
        progress_bar.finish()

    @contextmanager
    def install_sub_bar(self, progress_bar):
        """Context manager for starting and positioning sub progress bars"""
        # Request a free slot
        slot = self._alloc_slot()
        if slot is not None:
            self._lines[slot] = progress_bar
            # Add line widgets
            progress_bar.widgets = ["%d: " % slot,] + progress_bar.widgets
            # Replace writer
            progress_bar.fd = Writer(self._get_sub_line(slot))
        else:
            # If no slots are free, redirect to /dev/null
            progress_bar.fd = open(os.devnull, 'w')
        # Make yellow
        progress_bar.widgets = [TERM.yellow, ] + progress_bar.widgets
        progress_bar.start()
        yield progress_bar
        progress_bar.finish()
        # Free the slot
        if slot is not None:
            self._free_slot(slot)

def bound_concurrency(size):
    """Decorator to limit concurrency on coroutine calls"""
    sem = asyncio.Semaphore(size)
    def decorator(func):
        """Actual decorator"""
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            """Wrapper"""
            async with sem:
                return await func(*args, **kwargs)
        return wrapper
    return decorator

# Bind concurrency at 10 executions
@bound_concurrency(10)
async def download_file(url, output_folder):
    """Download a file at url to the output path"""
    try:
        LOGGER.debug("Downloading file from uri: %r to folder: %r", url, output_folder)
        async with aiohttp.ClientSession() as session:
            # Request the file
            resp = await session.get(url)
            # Extract the basename, for cases where the filename is unknown
            basename = os.path.basename(url)
            # Attempt to extract file name from the Content-Disposition header
            content_dispo = resp.headers.get('Content-Disposition', None)
            if content_dispo:
                # Parse disposition and extract filename
                _, params = cgi.parse_header(content_dispo)
                filename = params.get('filename')
                LOGGER.debug("Got filename %r for basename %r", filename, basename)
            else:
                # If the content disposition is not available, just use the basename from the url
                filename = basename
            # Attempt to get the file size from the Content-Length header
            content_length = resp.headers.get('Content-Length', None)
            file_size = int(content_length) if content_length else None
            # Create output file
            async with aiofiles.open(pathlib.Path(output_folder, filename), "wb") as output_file:
                # Create progress bar for this file
                chunks_bar = progressbar.ProgressBar(
                    widgets=[filename,] + SUB_BAR_WIDGETS,
                    maxval=file_size)
                chunks_bar.term_width = int(chunks_bar.term_width * 0.6)
                # Install to sub progress bar
                with PROGRESS_MANAGER.install_sub_bar(chunks_bar):
                    # Iterate over chunks and write them to file
                    async for chunk, _ in resp.content.iter_chunks():
                        if file_size:
                            # If we have the file size, update the file
                            chunks_bar.update(chunks_bar.currval + len(chunk))
                        await output_file.write(chunk)
    except (aiohttp.client_exceptions.ClientError, asyncio.TimeoutError) as exc:
        LOGGER.error("Could not download file %r - %r", url, exc)
    except OSError as exc:
        LOGGER.error("Could not write file - %r", exc)

async def fetch(url):
    """Fetch a page and return the response"""
    async with aiohttp.ClientSession() as session:
        LOGGER.debug("Fetching url %r", url)
        resp = await session.get(url)
        LOGGER.debug("Got response, status %d", resp.status)
        resp.raise_for_status()
        return await resp.text()

# pylint: disable=too-few-public-methods
class Writer(object):
    """Create an object with a write method that writes to a
    specific place on the screen, defined at instantiation.
    This is the glue between blessings and progressbar.
    """
    # Taken from https://github.com/aaren/multi_progress
    def __init__(self, location):
        """Input: location - tuple of ints (x, y), the position of the bar in the terminal"""
        self.location = location
    def write(self, string):
        """Write with saved location"""
        with TERM.location(*self.location):
            print(string)

async def scrape():
    """Scrape"""
    url = "http://speedtest.tele2.net/"
    try:
        resp_text = await fetch(url)
    except (aiohttp.client_exceptions.ClientError, asyncio.TimeoutError) as exc:
        LOGGER.error("Could not fetch page - %r", exc)
        return
    # Make soup
    LOGGER.info("Extracting 100MB link")
    soup = BeautifulSoup(resp_text, 'html.parser')
    # Filter all links according to their target's prefix.
    all_files = list(filter(lambda element: element.attrs.get('href', '').endswith('.zip'),
                            soup.findAll("a", href=True)))
    LOGGER.info("Got %d zip links", len(all_files))

    all_urls = map(lambda element: element.attrs['href'], all_files)
    target_url = list(filter(lambda href: href.startswith("100MB"), all_urls))[0]
    coros_bar = progressbar.ProgressBar(
        widgets=MAIN_BAR_WIDGETS,
        maxval=1)
    coros_bar.term_width = int(coros_bar.term_width * 0.6)
    with PROGRESS_MANAGER.install_main(coros_bar):
        await download_file(url + target_url, "/tmp/")

def print_banner():
    """Print the colorful blinking banner"""
    print(TERM.magenta + TERM.blink + TERM.bold + BANNER + TERM.normal)

def main():
    """Main function"""
    # pylint: disable=global-statement
    global PROGRESS_MANAGER
    # Enter fullscreen hidden cursor mode
    with TERM.fullscreen(), TERM.hidden_cursor():
        # Instantiate the progress manager
        PROGRESS_MANAGER = ProgressBarManager(TERM, main_coords=(0, len(BANNER.splitlines()) + 1))
        print_banner()
        # Start work
        print("\nStarting...")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(scrape())

TERM = blessings.Terminal()
PROGRESS_MANAGER = None

if __name__ == "__main__":
    # Set up logging
    LOGGER = logging.getLogger("scraper")
    LOG_LEVEL = logging.WARNING
    logging.basicConfig(level=LOG_LEVEL)
    coloredlogs.install(level=LOG_LEVEL, LOGGER=LOGGER)
    main()

#!/bin/env python3
# PIP
import argparse
# Async
import asyncio
import json
import logging
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from configparser import ConfigParser
from io import IOBase
from pathlib import Path
# Builtins
from typing import Optional

import aiofiles
import aiolimiter
import feedparser
import httpx


# ----- ----- ----- ----- -----

class NoOpRateLimiter():

  async def __aenter__(self):
    return

  async def __aexit__(self, exc_type, exc, tb):
    return

class Countdown:
  def __init__(self, limit: int):
    self.value = limit
    self.lock = asyncio.Lock()

  async def decrement(self):
    async with self.lock:
      if self.value <= 0:
        raise ValueError("Reached limit")
      self.value -= 1


class podqueue():

  # ----- ----- ----- ----- -----
  # RUN-ONCE
  # ----- ----- ----- ----- -----


  def __init__(self):
    # Initialise to defaults before checking config file / CLI args
    self.verbose = False
    self.opml = None
    self.dest = os.path.join(os.getcwd(), 'output')
    self.time_format = '%Y-%m-%d'
    self.log_file = 'podqueue.log'
    self.feeds = []
    self.FEED_FIELDS = ['title', 'link', 'description', 'published', 'image', 'categories',]
    self.EPISODE_FIELDS = ['title', 'link', 'description', 'published_parsed', 'links',]
    self.http_session: Optional[httpx.AsyncClient] = None
    self.rate_limit = None
    self.downloads_per_feed = None

    # If a config file exists, ingest it
    self.check_config()

    # Overwrite any config file defaults with CLI params
    self.cli_args()

    if not self.rate_limit:
      self.rate_limiter = NoOpRateLimiter()
    else:
      self.rate_limiter = aiolimiter.AsyncLimiter(self.rate_limit, 60)

    if self.downloads_per_feed is None or self.downloads_per_feed < 1:
        self.downloads_per_feed = float("inf")

    self.config_logging()

    # Check an OPML was provided
    try:
      assert self.opml is not None
    except Exception as e:
      logging.error('OPML file or destination dir was not provided')
      exit()


  def config_logging(self) -> None:

    # Always log to file; only stdout if -v
    handlers = [logging.FileHandler(self.log_file)]
    if (self.verbose): handlers.append(logging.StreamHandler())

    # Config settings
    level = logging.INFO if (self.verbose) else logging.WARNING
    logging.basicConfig(level=level, datefmt='%Y-%m-%d %H:%M:%S', handlers=handlers,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    # Add header for append-mode file logging
    logging.info('\n----- ----- ----- ----- -----\nInitialising\n----- ----- ----- ----- -----')


  def ascii_normalise(self, input_str: str) -> str:
    try:
      # Replace non-simple chars with unders
      input_str = re.sub(r'[^a-zA-Z0-9\-\_\/\\\.]', '_', input_str)
      # Replace any strings of 2+ puncts with a single underscore
      input_str = re.sub(r'_+', r'_', input_str)
      input_str = re.sub(r'([^a-zA-Z0-9]{2,})', r'_', input_str)
      # Remove any trailing puncts
      input_str = re.sub(r'(_|\.)$', r'', input_str)
      
    except Exception as e:
      logging.error(f'\t\tError normalising file name: {e}')
      exit()

    return input_str


  def check_config(self) -> None:
    # get the path to podqueue.conf
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'podqueue.conf')

    # Check if the file has been created
    if not os.path.exists(config_path):
      logging.info(f'Config file does not exist: {config_path}')
      return None

    conf = ConfigParser()
    conf.read(config_path)

    for key in ['opml', 'dest', 'time_format', 'verbose', 'log_file']:
      if conf['podqueue'].get(key, None):
        setattr(self, key, conf['podqueue'].get(key, None))

    # If we just changed verbose to str, make sure it's back to a bool
    if self.verbose:
      self.verbose = bool(self.verbose)


  def cli_args(self) -> None:
    parser = argparse.ArgumentParser(add_help=True)

    parser.add_argument('-o', '--opml', dest='opml', default=None, type=argparse.FileType('r'),
      help='Pass an OPML file that contains a podcast subscription list.')
    parser.add_argument('-d', '--dest', dest='dest', type=self.args_path,
      help='The destination folder for downloads. Will be created if required, including sub-directories for each separate podcast.')
    parser.add_argument('-t', '--time_format', dest='time_format',
      help='Specify a time format string for JSON files. Defaults to 2022-06-31 if not specified.')
    parser.add_argument('-v', '--verbose', default=False, action='store_true',
      help='Prints additional debug information. If excluded, only errors are printed (for automation).')
    parser.add_argument('-l', '--log_file', dest='log_file',
      help='Specify a path to the log file. Defaults to ./podqueue.log')
    parser.add_argument('-r', '--rate_limit', dest='rate_limit', type=int, help="Rate limit in network 'actions' per minute. Defaults to 0 (no limit).", default=0)
    parser.add_argument('-n', '--downloads-per-feed', dest='downloads_per_feed', type=int, help="Number of episodes to download per feed. Defaults to 0 (all).", default=0)
    
    # Save the CLI args to class vars - self.XXX
    # vars() converts into a native dict
    result = vars(parser.parse_args())
    for key, value in result.items():
      # Don't overwrite if it's not provided in CLI
      if value is not None:
        setattr(self, key, value)


  def args_path(self, directory: str) -> str:
    # Create the directory, if required
    if not os.path.isdir(directory):
      os.makedirs(directory)

    return directory


  def parse_opml(self, opml) -> None:
    logging.info(f'Parsing OPML file: {opml.name}')

    # Check if we have an actual file handle (CLI arg), 
    # Or a string path (config file), and we need to get our own handle
    with (opml if isinstance(opml, IOBase) else open(opml, 'r')) as opml_f:
      xml_root = ET.parse(opml_f).getroot()

    # Get all RSS feeds with a 'xmlUrl' attribute
    for feed in [x.attrib for x in xml_root.findall(".//outline[@type='rss']")]:
      feed_url = feed.get('xmlUrl', None)
      if feed_url:
        self.feeds.append(feed_url)


  # ----- ----- ----- ----- -----
  # PER-FEED
  # ----- ----- ----- ----- -----


  async def get_feed(self, feed: str) -> None:
    logging.info(f'Fetching feed: {feed}')
    
    try:
      html_response = await self.http_session.get(feed, 
        follow_redirects=True
      )
      html_response.raise_for_status()
      html = html_response.text
    # The remote RSS server can close the HTTP connection
    except Exception as error:
      logging.warning(f'Error fetching RSS feed for: {feed}, {error=}')
      return None

    content = feedparser.parse(html)

    # If feedparser library reports bad XML, warn and skip
    # CharacterEncodingOverride is a false positive, ATP for example
    # Test str: 'http://feedparser.org/tests/illformed/rss/aaa_illformed.xml'
    if content.get('bozo', False) and not isinstance(
      content.bozo_exception, 
      feedparser.exceptions.CharacterEncodingOverride
    ):
      logging.warning(f'Feed is misformatted: {feed}, {content.bozo_exception}')
      return None

    title = content.feed.get('title', 'Unknown Title')
    logging.info(f'\tProcessing feed: {title}')

    directory = self.create_feed_directories(title)

    # Get content.feed metadata - podcast title, icon, description, etc.
    # And write it to disk as <<PODCAST>>/<<PODCAST>>.json
    feed_metadata = await self.write_feed_metadata(content, directory)

    # Also fetch the podcast logo, if available
    if feed_metadata.get('image', None):
      await self.write_feed_image(feed_metadata['image'], directory)

    countdown = Countdown(self.downloads_per_feed)

    # Then, process the episodes each and write to disk
    for episode in content.entries:
      await self.process_feed_episode(episode, directory, countdown)


  def create_feed_directories(self, title: str) -> str:
    # Normalise the podcast name with no spaces or non-simple ascii
    feed_dir_name = '_'.join([x for x in title.split(' ')])
    feed_dir_name = self.ascii_normalise(feed_dir_name)

    # Create the directory we need (no spaces) if it doesn't exist
    directory = os.path.join(self.dest, feed_dir_name)
    if not os.path.exists(directory) or not os.path.isdir(directory):
      os.makedirs(directory)

    # Also create the <<PODCAST>>/episodes subdirectory
    if not os.path.isdir(os.path.join(directory, 'episodes')):
      os.makedirs(os.path.join(directory, 'episodes'))
    
    return directory


  async def write_feed_metadata(self, 
              content: feedparser.util.FeedParserDict, 
              directory: str) -> dict:
    logging.info(f'\t\tProcessing feed metadata')
    
    feed_metadata = {}

    for field in self.FEED_FIELDS:
      # .image is a dict structure where we only want href, 
      # the rest are strs, so special case
      if (field == 'image') and (content.feed.get('image', None)):
        value = content.feed.image.href
      else:
        value = content.feed.get(field, None)

      feed_metadata[field] = value

    # Additional calculated metadata based on structure:
    feed_metadata['episode_count'] = len(content.entries)

    metadata_filename = os.path.join(directory, f'{os.path.split(directory)[1]}.json')

    async with aiofiles.open(metadata_filename, 'w') as meta_f:
      await meta_f.write(json.dumps(feed_metadata))

    return feed_metadata


  async def write_feed_image(self, image_url: str, directory: str) -> None:
    image_filename_ext = os.path.splitext(image_url)[1]
    image_filename_ext = image_filename_ext if image_filename_ext else '.jpg'
    image_filename = os.path.join(directory, f'{os.path.split(directory)[1]}{image_filename_ext}')

    async with self.http_session.stream('GET', 
      image_url,
      follow_redirects=True
      ) as response:

      response.raise_for_status()
      
      async with aiofiles.open(image_filename, 'wb') as img_f:
        async for chunk in response.aiter_bytes(chunk_size=1024*8):
          await img_f.write(chunk)

    logging.info(f'\t\tAdded image to disk: {os.path.split(image_filename)[1]}')


  # ----- ----- ----- ----- -----
  # PER-EPISODE
  # ----- ----- ----- ----- -----


  async def process_feed_episode(self, 
              episode: feedparser.util.FeedParserDict, 
              directory: str,
              countdown: Countdown) -> None:
    episode_metadata = {}
    for field in self.EPISODE_FIELDS:
      episode_metadata[field] = episode.get(field, None)

    # Change the time_struct tuple to a human string
    if episode_metadata.get('published_parsed', None):
      episode_metadata['published_parsed'] = time.strftime(self.time_format, \
                                          episode_metadata['published_parsed'])

    # Get a unique episode filename(s)
    episode_title = f'{episode_metadata["published_parsed"]}_{episode_metadata["title"]}'

    # Special case - the final file name (not path) can't have a slash in it
    # Also replace colons as they are invalid in filenames on Windows ...
    # ... (used for Alternate Data Streams on NTFS)
    episode_title = re.sub(r'(\/|\\|:|\?|")', r'_', episode_title)
    episode_title = self.ascii_normalise(episode_title)

    # Check the title isn't going to overshoot 255 bytes
    # This is the limit in ZFS, BTRFS, ext*, NTFS, APFS, XFS, etc ...
    # Otherwise, file.write will raise OSError 36 - "File name too long"
    # I'm looking at you, Memory Palace 73. I mean really, 55 words and 316 characters long?
    # https://thememorypalace.us/notes-on-an-imagined-plaque/
    if len(episode_title) >= 250:
      episode_title = f'{episode_title[0:245]}_'
    
    episode_meta_filename = os.path.join(os.path.join(directory, 'episodes'), \
                        f'{episode_title}.json')
    episode_audio_filename = os.path.join(os.path.join(directory, 'episodes'), \
                        f'{episode_title}.mp3')

    # Check if the file already exists on disk (if so, skip)
    if os.path.exists(episode_meta_filename) and os.path.exists(episode_audio_filename):
      logging.info(f'\t\tEpisode already saved, skipping: {episode_title}')
      return None

    try:
      await countdown.decrement()
    except ValueError:
      logging.info(f'\t\tReached download limit for feed, skipping: {episode_title}')
      return None

    episode_metadata = await self.write_episode_metadata(episode_title, 
      episode_metadata, episode_meta_filename
    )

    if episode_metadata.get('link', None):
      await self.write_episode_audio(episode_title, 
        episode_metadata.get('link'), episode_audio_filename
      )


  async def write_episode_metadata(self, 
              episode_title: str, 
              episode_metadata: dict, 
              episode_meta_filename: str) -> dict:
    # Change the links{} into a single audio URL
    if episode_metadata.get('links', None):
      for link in episode_metadata['links']:
        if link.get('type', None):
          if 'audio' in link.get('type', None):
            episode_metadata['link'] = link.get('href', None)
            break

      # Remove the old complicated links{}
      episode_metadata.pop('links', None)

    async with aiofiles.tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_ep_meta_f:
        await temp_ep_meta_f.write(json.dumps(episode_metadata))
    shutil.move(temp_ep_meta_f.name, episode_meta_filename)

    logging.info(f'\t\tAdded episode metadata to disk: {episode_title}')
    return episode_metadata


  async def write_episode_audio(self, 
              episode_title: str, 
              audio_url: str,
              episode_audio_filename: str) -> None:
    async with aiofiles.tempfile.NamedTemporaryFile(mode='wb', delete=False) as temp_audio_f:
      temp_audio_path = Path(temp_audio_f.name)
      process = await asyncio.create_subprocess_exec('aria2c',
                                         '--max-connection-per-server=4',
                                         '--show-console-readout=false',
                                         '--summary-interval=10',
                                         '--allow-overwrite=true',
                                         '--auto-file-renaming=false',
                                         '--max-download-limit=2M',
                                         f'--dir={temp_audio_path.parent}',
                                         f'--out={temp_audio_path.name}',
                                         audio_url)
      rc = await process.wait()
      if rc != 0:
        logging.error(f'\t\tError downloading episode audio: {episode_title} from {audio_url}')
        os.unlink(temp_audio_f.name)
        return None
      # Move the file to the final location
      shutil.move(temp_audio_f.name, episode_audio_filename)

    # async with self.http_session.stream('GET',
    #   audio_url,
    #   follow_redirects=True
    #   ) as response:
    #
    #     response.raise_for_status()
    #
    #     async with aiofiles.tempfile.NamedTemporaryFile(mode='wb', delete=False) as temp_audio_f:
    #       last_report_time = time.time()
    #       async for chunk in response.aiter_bytes(chunk_size=1024*8):
    #         async with self.rate_limiter:
    #           await temp_audio_f.write(chunk)
    #           if (time.time() - last_report_time) > 60:
    #             logging.info(f'\t\t\t{episode_title}: {humanize.filesize.naturalsize(await temp_audio_f.tell(), binary=True)}')
    #             last_report_time = time.time()
    #
    #     shutil.move(temp_audio_f.name, episode_audio_filename)

    logging.info(f'\t\tAdded episode audio to disk: {episode_title}')


# ----- ----- ----- ----- -----

async def entry():
  # Initialise the config - from file, or CLI args
  pq = podqueue()
  
  # Parse all feed URLs out of the OPML XML into pq.feeds=[]
  pq.parse_opml(pq.opml)

  async with httpx.AsyncClient() as http_session:
    pq.http_session = http_session

    # Download the metadata, images, and any missing episodes
    tasks = [asyncio.create_task(
              pq.get_feed(feed))
            for feed in pq.feeds]

    done, pending = await asyncio.wait(tasks)
    logging.info('Async {done=}, {pending=}')


if __name__ == '__main__':
  asyncio.run(entry())

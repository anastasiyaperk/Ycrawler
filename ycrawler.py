import argparse
import asyncio
from asyncio import AbstractEventLoop
import csv
from datetime import datetime
import html
import logging
import os
import re
from typing import List

import aiohttp
from aiohttp import ClientSession
import async_timeout

URL_TEMPLATE = "https://hacker-news.firebaseio.com/v0/item/{}.json"
STORY_URL_TEMPLATE = "https://news.ycombinator.com/item?id={}"
TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
FETCH_TIMEOUT = 10
REFERENCES_REGEXP = r'<a[^>]* href="([^"]*)"'
COLLECTED_STORIES = []
REPORT_FILE = r"report.csv"

logging.basicConfig(format="[%(asctime)s] %(levelname).1s %(message)s", datefmt="%Y.%m.%d %H:%M:%S")
logger = logging.getLogger()


class URLFetcher:
    """
    Provides counting of URL fetches for a particular task.
    """

    def __init__(self):
        self.fetch_counter = 0

    async def fetch(self, session, url, dest_dir=None, get_response=True, ):
        """
        Fetch a URL using aiohttp returning parsed JSON response or saving to file.
        As suggested by the aiohttp docs we reuse the session.
        """
        async with async_timeout.timeout(FETCH_TIMEOUT):
            self.fetch_counter += 1
            try:
                async with session.get(url) as response:
                    if dest_dir:
                        save_content_to_disk(await response.read(), url, dest_dir)
                    elif get_response:
                        return await response.json()

            except asyncio.TimeoutError:
                logger.error(f"Timeout error on url: {url}")


def save_content_to_disk(resp_bytes, url, dest_dir):
    """
    Save collected from url content to disk

    :param resp_bytes: bytes of content
    :param url: url
    :param dest_dir: destination directory
    :return:
    """
    if resp_bytes is None:
        logger.debug(f"Empty content on {url}")
        return

    filename = re.sub(r"[?:*<>,; /\\]", r'', url)
    full_filename = os.path.join(dest_dir, filename[:20])
    with open(full_filename, "wb+") as f:
        f.write(resp_bytes)


def get_path_of_story(dest_dir, story_title, story_id) -> str:
    """
    Get path to story directory or create if does not exist

    :param dest_dir: destination dir of all stories
    :param story_title: story title
    :param story_id: story id
    :return: path of story directory
    """
    dir_name = str(story_id)
    path = os.path.join(dest_dir, dir_name)
    if not os.path.exists(path):
        # create dir if does nor exist
        os.makedirs(path)
        COLLECTED_STORIES.append(story_id)
        with open(os.path.join(dest_dir, REPORT_FILE), 'a+', encoding='UTF8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([story_id, story_title])

    return path


def load_processed_stories_list(story_dir: str) -> List[int]:
    """
    Load processed stories ids from report file

    :param story_dir: path to directory with all stories
    :return: total list of all processed stories ids
    """
    path_to_file = os.path.join(story_dir, REPORT_FILE)
    total_ids = []
    if os.path.exists(path_to_file):
        with open(path_to_file, 'r', encoding='UTF8') as f:
            reader = csv.reader(f)
            for row in reader:
                total_ids.append(int(row[0]))

    return total_ids


async def get_page_with_references(loop_: AbstractEventLoop,
                                   session: ClientSession,
                                   fetcher: URLFetcher,
                                   post_id,
                                   story_dir):
    """
    Retrieve data for current post and recursively for all comments.

    :param loop_: event loop for running
    :param session: tcp client session
    :param fetcher: url fetcher
    :param post_id: id if post
    :param story_dir: directory of collected stories
    :return: number_of_comments, number_of_refs
    """
    url = URL_TEMPLATE.format(post_id)
    number_of_refs = 0
    response = await fetcher.fetch(session, url)

    # base case, there are no comments
    if response is None or "kids" not in response:
        return 0, number_of_refs

    elif response.get("type") == "story":
        logger.debug("Get story response")
        story_url = response.get("url", STORY_URL_TEMPLATE.format(post_id))
        story_dir = get_path_of_story(story_dir, response.get("title", "untitled"), post_id)
        await fetcher.fetch(session, story_url, dest_dir=story_dir)

    elif response.get('type') == "comment":
        logger.debug("Get comment response")
        refs = set(re.findall(REFERENCES_REGEXP, html.unescape(response.get("text", ""))))
        number_of_refs = len(refs)
        tasks = [fetcher.fetch(session, comment_url, dest_dir=story_dir) for comment_url in refs]
        await asyncio.gather(*tasks)

    # calculate this post's comments as number of comments
    number_of_comments = len(response["kids"])

    # create recursive tasks for all comments
    tasks = [get_page_with_references(
        loop_, session, fetcher, kid_id, story_dir) for kid_id in response["kids"]]

    # schedule the tasks and retrieve results
    results = await asyncio.gather(*tasks)

    # reduce the descendents comments and add it to this post's
    number_of_comments += sum([res[0] for res in results])
    number_of_refs += sum([res[1] for res in results])

    return number_of_comments, number_of_refs


async def get_top_stories_with_references(loop_: AbstractEventLoop,
                                          limit: int,
                                          path: str,
                                          connections_limit) -> int:
    """
    Retrieve top stories in HN

    :param loop_: event loop for running
    :param limit: max count of stories
    :param path: destination dir for collecting stories
    :param connections_limit: connections_limit for TCP client
    :return: num of fetches
    """
    connector = aiohttp.TCPConnector(limit_per_host=connections_limit, loop=loop_)
    async with aiohttp.ClientSession(connector=connector) as session:
        # create a new fetcher for this task
        fetcher = URLFetcher()

        try:
            response = await fetcher.fetch(session, TOP_STORIES_URL)
        except Exception as ex:
            logger.error(f"Error retrieving top stories: {ex}")
            raise

        tasks = [get_page_with_references(
            loop_, session, fetcher, post_id, path) for post_id in response[:limit] if
            post_id not in COLLECTED_STORIES]

        try:
            results = await asyncio.gather(*tasks)
        except Exception as ex:
            logger.error(f"Error retrieving comments for top stories: {ex}")
            raise

        for post_id, num_comments in zip(response[:limit], results):
            logger.info(f"Post {post_id} has {num_comments} comments")

        logger.debug(f"Complete loading top stories with {fetcher.fetch_counter} fetches")
        return fetcher.fetch_counter


async def poll_top_stories(loop_: AbstractEventLoop,
                           period: int,
                           limit: int,
                           path: str,
                           connections_limit: int):
    """
    Scheduling of get_top_stories_with_references.

    :param loop_: event loop for running
    :param period: Waiting period in seconds
    :param limit: max count of stories
    :param path: destination dir for collecting stories
    :param connections_limit: connections_limit for TCP client
    :return:
    """
    iterations = 0
    while True:
        logger.info(f"Downloading content for top {limit} stories (iteration = {iterations})")
        iterations += 1
        now = datetime.now()
        try:
            fetch_count = await get_top_stories_with_references(loop_,
                                                                limit,
                                                                path,
                                                                connections_limit)
            total_time = (datetime.now() - now).total_seconds()
            logger.info(f"The downloading took {total_time:.3f} seconds and {fetch_count} fetches")

        except Exception as ex:
            logger.error(f"Unexpected exception in poll_top_stories: {ex}")

        iterations += 1
        logger.info(f"Waiting ({period} sec.) ... ")
        await asyncio.sleep(period)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog="Ycrawler", description="Collecting top news from news.ycombinator.com")

    parser.add_argument("--period", type=int, default=30,
                        help="Period (in sec) of running poll")
    parser.add_argument("--limit", type=int, default=30,
                        help="Limit of collected news")
    parser.add_argument("--verbose", action='store_true', default=False,
                        help="Flag of dry run. If True, use log level - DEBUG")
    parser.add_argument("--path", type=str, default="./data",
                        help="Path to folder, where collected news will stored")
    parser.add_argument("--connections_limit", type=int, default=3,
                        help="The limit for connections")
    parser.add_argument("--log", type=str, default="crawler.log",
                        help="Name of logfile")
    args = parser.parse_args()

    fh = logging.FileHandler(args.log)
    fh.setLevel(logging.INFO if not args.verbose else logging.DEBUG)
    logger.addHandler(fh)

    COLLECTED_STORIES = load_processed_stories_list(args.path)

    logger.info(f"Ycrawler started with options: {args.__dict__}")
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(poll_top_stories(loop, args.period, args.limit, args.path, args.connections_limit))

    except Exception as ex:
        logger.error(f"Unexpected exception: {ex}", exc_info=True)

    finally:
        loop.close()

"""
Collect images from the auth token owner's new retweets,
by polling the user's timeline.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from nazurin import bot, config, dp
from nazurin.database import Database
from nazurin.utils import logger
from nazurin.utils.exceptions import (
    AlreadyExistsError,
    InvalidCommandUsageError,
    NazurinError,
)
from nazurin.utils.filters import IDFilter
from nazurin.utils.helpers import format_error

from .api import Twitter
from .api.web import RateLimit, WebAPI
from .config import AUTH_TOKEN, RETWEET_INTERVAL, RETWEET_USER_ID, WATCH_RETWEETS

DOCUMENT = "twitter_retweet_watcher"
# Safety limit of pages to look back, X serves about 3200 tweets at most
MAX_PAGES = 50
# Seconds between timeline pages
PAGE_DELAY = 1
# Wait for the rate limit to reset when fewer requests than this are left
MIN_RATE_LIMIT_REMAINING = 2
MAX_RATE_LIMIT_WAIT = 16 * 60
# Seconds between collections when backfilling, to avoid hitting rate limits
BACKFILL_DELAY = 3
MAX_REPORTED_FAILURES = 20
# Dates without time zone given to the backfill command
LOCAL_TIMEZONE = timezone(timedelta(hours=8))
TWITTER_EPOCH_MS = 1288834974657


def snowflake_from_datetime(moment: datetime) -> int:
    """Smallest tweet ID that could be created at `moment`."""
    return (int(moment.timestamp() * 1000) - TWITTER_EPOCH_MS) << 22


def datetime_from_snowflake(snowflake: int) -> datetime:
    return datetime.fromtimestamp(
        ((snowflake >> 22) + TWITTER_EPOCH_MS) / 1000,
        tz=LOCAL_TIMEZONE,
    )


def parse_since(text: str) -> datetime:
    """Parse "2026-10-28" or "2026-10-28T12:00", in local time if no time zone."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TIMEZONE)
    return moment


def describe(error: Exception) -> str:
    # Messages are sent in HTML parse mode
    if isinstance(error, NazurinError):
        return escape(str(error), quote=False)
    return format_error(error)


def status_url(tweet_id: str) -> str:
    return f"https://x.com/i/web/status/{tweet_id}"


def tweet_id(result: dict) -> int:
    return int(WebAPI._unwrap_visibility_result(result).get("rest_id", 0))


def has_media(tweet: dict) -> bool:
    return bool(tweet.get("legacy", {}).get("extended_entities", {}).get("media"))


def collectable_urls(result: dict) -> list[str]:
    """
    URLs to collect for a timeline tweet: the retweeted tweet, and the tweet it
    quotes, if any, as long as they have media. The user's own tweets and
    quotes are ignored.
    """

    unwrap = WebAPI._unwrap_visibility_result
    tweet = unwrap(result)
    retweeted = tweet.get("legacy", {}).get("retweeted_status_result", {})
    if "result" not in retweeted:
        return []
    original = unwrap(retweeted["result"])
    targets = [original]
    quoted = original.get("quoted_status_result", {})
    if "result" in quoted:
        targets.append(unwrap(quoted["result"]))
    return [status_url(target["rest_id"]) for target in targets if has_media(target)]


def parse_timeline(response: dict) -> tuple[list[dict], str | None]:
    """Return tweets on a UserTweets page and the cursor to the next page."""

    user = response["data"]["user"]["result"]
    timeline = (user.get("timeline") or user.get("timeline_v2"))["timeline"]
    tweets = []
    cursor = None
    for instruction in timeline["instructions"]:
        # Skip TimelinePinEntry, the pinned tweet isn't new
        if instruction.get("type") != "TimelineAddEntries":
            continue
        for entry in instruction["entries"]:
            content = entry["content"]
            if content.get("cursorType") == "Bottom":
                cursor = content["value"]
                continue
            if "itemContent" in content:
                items = [content["itemContent"]]
            else:
                # Conversation module, e.g. a thread of the user's own tweets
                items = [
                    item["item"]["itemContent"] for item in content.get("items", [])
                ]
            results = (item.get("tweet_results", {}).get("result") for item in items)
            tweets.extend(result for result in results if result)
    return tweets, cursor


class RetweetWatcher:
    def __init__(self):
        self.api = Twitter.web
        self.user_id = RETWEET_USER_ID
        self.task: asyncio.Task | None = None
        self.backfill_task: asyncio.Task | None = None
        self.last_error: str | None = None
        # Polling and backfilling may collect the same tweets, don't run together
        self.lock = asyncio.Lock()

    @staticmethod
    def document():
        return Database().driver().collection(config.NAZURIN_DATA).document(DOCUMENT)

    async def load_last_id(self) -> int | None:
        data = await self.document().get()
        return int(data["last_id"]) if data else None

    async def save_last_id(self, last_id: int):
        data = {"last_id": str(last_id)}
        document = self.document()
        if await document.exists():
            await document.update(data)
        else:
            collection = Database().driver().collection(config.NAZURIN_DATA)
            await collection.insert(DOCUMENT, data)

    async def fetch_new_tweets(self, last_id: int | None) -> tuple[list[dict], bool]:
        """
        Fetch tweets newer than `last_id`, oldest first,
        and whether the timeline was read back to `last_id`.
        """

        tweets = []
        cursor = None
        for page_number in range(MAX_PAGES):
            if page_number:
                await asyncio.sleep(PAGE_DELAY)
            await self.wait_for_rate_limit("UserTweets")
            response = await self.api.user_tweets(self.user_id, cursor)
            page, next_cursor = parse_timeline(response)
            tweets += [tweet for tweet in page if tweet_id(tweet) > (last_id or 0)]
            # Stop once reaching tweets seen before; on first run, only take a look
            reached = last_id is None or any(
                tweet_id(tweet) <= last_id for tweet in page
            )
            # Also stop at the end of the timeline, or if the cursor stops moving
            if reached or not page or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return sorted(tweets, key=tweet_id), reached

    @staticmethod
    async def wait_for_rate_limit(operation: str):
        """Wait for the rate limit of `operation` to reset if it's almost reached."""

        limit: RateLimit | None = WebAPI.rate_limits.get(operation)
        if not limit or limit.remaining >= MIN_RATE_LIMIT_REMAINING:
            return
        delay = min(limit.reset - time.time() + 1, MAX_RATE_LIMIT_WAIT)
        if delay <= 0:
            return
        logger.info("{} rate limit almost reached, waiting {:.0f}s", operation, delay)
        await asyncio.sleep(delay)

    async def ensure_user_id(self):
        if not self.user_id:
            self.user_id = await self.api.viewer_id()
            logger.info("Watching retweets of user {}", self.user_id)

    async def poll(self):
        await self.ensure_user_id()
        last_id = await self.load_last_id()
        tweets, complete = await self.fetch_new_tweets(last_id)
        if not tweets:
            if not complete:
                # Raised to be reported once until it recovers
                raise NazurinError("Timeline returned no tweets")
            return
        if last_id is None:
            # First run, start from now instead of collecting past retweets
            await self.save_last_id(tweet_id(tweets[-1]))
            logger.info("Retweet watcher started from tweet {}", tweet_id(tweets[-1]))
            return
        if not complete:
            since = datetime_from_snowflake(last_id).strftime("%Y-%m-%dT%H:%M")
            until = datetime_from_snowflake(tweet_id(tweets[0])).strftime(
                "%Y-%m-%dT%H:%M"
            )
            await self.notify(
                "Retweet watcher couldn't read the timeline back to the last "
                f"seen tweet, retweets made between {since} and {until} may be "
                f"missed. Send /retweet_backfill {since} to collect them.",
            )
        for tweet in tweets:
            for url in collectable_urls(tweet):
                if error := await self.collect(url):
                    await self.notify(f"Error collecting retweet {url}: {error}")
            await self.save_last_id(tweet_id(tweet))

    async def collect(self, url: str) -> str | None:
        """Collect `url`, return the error message if failed."""

        logger.info("Collecting retweet {}", url)
        await self.wait_for_rate_limit("TweetDetail")
        try:
            await bot.update_collection([url])
        except AlreadyExistsError:
            logger.info("Retweet {} already collected", url)
        # pylint: disable-next=broad-exception-caught
        except Exception as error:
            logger.exception("Error collecting retweet {}", url)
            return describe(error)
        return None

    async def backfill(self, since: datetime) -> str:
        """
        Collect retweets made since `since`, return a report.
        Already collected ones are skipped by the database check.
        """

        async with self.lock:
            await self.ensure_user_id()
            tweets, complete = await self.fetch_new_tweets(
                snowflake_from_datetime(since),
            )
            urls = [url for tweet in tweets for url in collectable_urls(tweet)]
            failures = []
            for url in urls:
                if error := await self.collect(url):
                    failures.append(f"{url}: {error}")
                await asyncio.sleep(BACKFILL_DELAY)
        report = (
            f"Retweet backfill since {since.isoformat()} finished.\n"
            f"Tweets checked: {len(tweets)}, retweeted media: {len(urls)}, "
            f"failed: {len(failures)}"
        )
        if not complete:
            report += (
                f"\nCouldn't read the timeline back to {since.isoformat()}, "
                "older retweets were not checked."
            )
        if failures:
            report += "\n\n" + "\n".join(failures[:MAX_REPORTED_FAILURES])
            if len(failures) > MAX_REPORTED_FAILURES:
                report += f"\n...and {len(failures) - MAX_REPORTED_FAILURES} more"
        return report

    async def run_backfill(self, since: datetime):
        try:
            report = await self.backfill(since)
        # pylint: disable-next=broad-exception-caught
        except Exception as error:
            logger.exception("Retweet backfill failed")
            report = f"Retweet backfill failed: {describe(error)}"
        await self.notify(report)

    def start_backfill(self, since: datetime) -> bool:
        if self.backfill_task and not self.backfill_task.done():
            return False
        self.backfill_task = asyncio.create_task(self.run_backfill(since))
        return True

    async def run(self):
        while True:
            try:
                async with self.lock:
                    await self.poll()
                if self.last_error:
                    self.last_error = None
                    await self.notify("Retweet watcher recovered.")
            # pylint: disable-next=broad-exception-caught
            except Exception as error:
                await self.on_poll_error(error)
            await asyncio.sleep(RETWEET_INTERVAL)

    async def on_poll_error(self, error: Exception):
        logger.exception("Retweet watcher failed to poll")
        # Query ID may have changed, discover it again next time
        self.api._user_tweets_operation.cache_clear()
        message = describe(error)
        # Only notify once until it recovers, instead of on every poll
        if message != self.last_error:
            self.last_error = message
            await self.notify(
                f"Retweet watcher error: {message}\n"
                "Identical errors will be suppressed until it recovers.",
            )

    @staticmethod
    async def notify(message: str):
        # Failing to notify must not stop the watcher
        try:
            await bot.send_message(config.ADMIN_ID, message)
        # pylint: disable-next=broad-exception-caught
        except Exception:
            logger.exception("Failed to send retweet watcher message")

    async def start(self, *_args):
        self.task = asyncio.create_task(self.run())

    async def stop(self, *_args):
        for task in (self.task, self.backfill_task):
            if task:
                task.cancel()


async def retweet_backfill(message: Message, command: CommandObject):
    try:
        since = parse_since(command.args.strip())
    except (AttributeError, ValueError) as error:
        raise InvalidCommandUsageError("retweet_backfill") from error
    if not watcher.start_backfill(since):
        await message.reply("A retweet backfill is already running.")
        return
    await message.reply(
        f"Backfilling retweets since {since.isoformat()}, "
        "a report will be sent when finished.",
    )


if WATCH_RETWEETS:
    if not AUTH_TOKEN:
        logger.warning("TWITTER_RETWEET_WATCH requires TWITTER_AUTH_TOKEN, ignored")
    else:
        watcher = RetweetWatcher()
        dp.startup.register(watcher.start)
        dp.shutdown.register(watcher.stop)
        dp.message_handler(
            IDFilter(config.ADMIN_ID),
            Command("retweet_backfill"),
            args="DATE",
            description="Collect retweets made since DATE",
            help_text=(
                "DATE is like 2026-10-28 or 2026-10-28T12:00, "
                "in UTC+8 unless a time zone is given."
            ),
        )(retweet_backfill)

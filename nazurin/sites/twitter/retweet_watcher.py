"""
Collect images from the auth token owner's new retweets,
by polling the user's timeline.
"""

import asyncio
from html import escape

from nazurin import bot, config, dp
from nazurin.database import Database
from nazurin.utils import logger
from nazurin.utils.exceptions import AlreadyExistsError, NazurinError
from nazurin.utils.helpers import format_error

from .api import Twitter
from .api.web import WebAPI
from .config import AUTH_TOKEN, RETWEET_INTERVAL, RETWEET_USER_ID, WATCH_RETWEETS

DOCUMENT = "twitter_retweet_watcher"
# Pages to look back each poll, in case of many retweets since the last poll
MAX_PAGES = 5


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
        self.last_error: str | None = None

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

    async def fetch_new_tweets(self, last_id: int | None) -> list[dict]:
        """Fetch tweets newer than `last_id`, oldest first."""

        tweets = []
        cursor = None
        for _ in range(MAX_PAGES):
            response = await self.api.user_tweets(self.user_id, cursor)
            page, cursor = parse_timeline(response)
            tweets += [tweet for tweet in page if tweet_id(tweet) > (last_id or 0)]
            # Stop once reaching tweets seen before; on first run, only take a look
            reached_seen = any(tweet_id(tweet) <= (last_id or 0) for tweet in page)
            if last_id is None or reached_seen or not page or not cursor:
                break
        return sorted(tweets, key=tweet_id)

    async def poll(self):
        if not self.user_id:
            self.user_id = await self.api.viewer_id()
            logger.info("Watching retweets of user {}", self.user_id)
        last_id = await self.load_last_id()
        tweets = await self.fetch_new_tweets(last_id)
        if not tweets:
            return
        if last_id is None:
            # First run, start from now instead of collecting past retweets
            await self.save_last_id(tweet_id(tweets[-1]))
            logger.info("Retweet watcher started from tweet {}", tweet_id(tweets[-1]))
            return
        for tweet in tweets:
            for url in collectable_urls(tweet):
                await self.collect(url)
            await self.save_last_id(tweet_id(tweet))

    async def collect(self, url: str):
        logger.info("Collecting retweet {}", url)
        try:
            await bot.update_collection([url])
        except AlreadyExistsError:
            logger.info("Retweet {} already collected", url)
        # pylint: disable-next=broad-exception-caught
        except Exception as error:
            logger.exception("Error collecting retweet {}", url)
            await self.notify(f"Error collecting retweet {url}: {describe(error)}")

    async def run(self):
        while True:
            try:
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
        if self.task:
            self.task.cancel()


if WATCH_RETWEETS:
    if not AUTH_TOKEN:
        logger.warning("TWITTER_RETWEET_WATCH requires TWITTER_AUTH_TOKEN, ignored")
    else:
        watcher = RetweetWatcher()
        dp.startup.register(watcher.start)
        dp.shutdown.register(watcher.stop)

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from nazurin.sites.twitter import retweet_watcher
from nazurin.sites.twitter.api.web import (
    MAIN_JS_PATTERN,
    USER_TWEETS_OPERATION_PATTERN,
    RateLimit,
    WebAPI,
)
from nazurin.sites.twitter.interface import patterns
from nazurin.sites.twitter.retweet_watcher import (
    TWITTER_EPOCH_MS,
    RetweetWatcher,
    collectable_urls,
    datetime_from_snowflake,
    parse_since,
    parse_timeline,
    snowflake_from_datetime,
    status_url,
)
from nazurin.utils.exceptions import NazurinError


@pytest.fixture(autouse=True)
def no_delays(monkeypatch):
    monkeypatch.setattr(retweet_watcher, "PAGE_DELAY", 0)
    monkeypatch.setattr(retweet_watcher, "BACKFILL_DELAY", 0)
    monkeypatch.setattr(WebAPI, "rate_limits", {})


def make_tweet(
    rest_id: str,
    *,
    media: bool = False,
    retweet_of: dict | None = None,
    quote_of: dict | None = None,
    wrapped: bool = False,
) -> dict:
    legacy = {"id_str": rest_id}
    if media:
        legacy["extended_entities"] = {"media": [{"type": "photo"}]}
    if retweet_of:
        legacy["retweeted_status_result"] = {"result": retweet_of}
    tweet = {"__typename": "Tweet", "rest_id": rest_id, "legacy": legacy}
    if quote_of:
        tweet["quoted_status_result"] = {"result": quote_of}
    if wrapped:
        return {"__typename": "TweetWithVisibilityResults", "tweet": tweet}
    return tweet


def make_page(tweets: list[dict], cursor: str | None = None) -> dict:
    entries = [
        {
            "entryId": f"tweet-{index}",
            "content": {"itemContent": {"tweet_results": {"result": tweet}}},
        }
        for index, tweet in enumerate(tweets)
    ]
    if cursor:
        entries.append(
            {
                "entryId": "cursor-bottom",
                "content": {"cursorType": "Bottom", "value": cursor},
            },
        )
    instructions = [
        {"type": "TimelinePinEntry", "entry": {}},
        {"type": "TimelineAddEntries", "entries": entries},
    ]
    return {
        "data": {
            "user": {
                "result": {"timeline": {"timeline": {"instructions": instructions}}}
            },
        },
    }


def test_retweet_with_media():
    tweet = make_tweet("2", retweet_of=make_tweet("1", media=True))
    assert collectable_urls(tweet) == [status_url("1")]


def test_retweet_without_media():
    tweet = make_tweet("2", retweet_of=make_tweet("1"))
    assert collectable_urls(tweet) == []


def test_own_tweets_and_quotes_are_ignored():
    assert collectable_urls(make_tweet("1", media=True)) == []
    own_quote = make_tweet("2", media=True, quote_of=make_tweet("1", media=True))
    assert collectable_urls(own_quote) == []


def test_retweeted_quote_collects_both_with_media():
    quote = make_tweet("2", media=True, quote_of=make_tweet("1", media=True))
    tweet = make_tweet("3", retweet_of=quote)
    assert collectable_urls(tweet) == [status_url("2"), status_url("1")]


def test_retweeted_quote_collects_only_layers_with_media():
    quote = make_tweet("2", quote_of=make_tweet("1", media=True))
    assert collectable_urls(make_tweet("3", retweet_of=quote)) == [status_url("1")]
    quote = make_tweet("2", media=True, quote_of=make_tweet("1"))
    assert collectable_urls(make_tweet("3", retweet_of=quote)) == [status_url("2")]


def test_wrapped_tweets_are_unwrapped():
    original = make_tweet("1", media=True, wrapped=True)
    tweet = make_tweet("2", retweet_of=original, wrapped=True)
    assert collectable_urls(tweet) == [status_url("1")]


def test_unavailable_original_is_skipped():
    tweet = make_tweet("2", retweet_of={"__typename": "TweetTombstone"})
    assert collectable_urls(tweet) == []


def test_parse_timeline():
    tweets = [make_tweet("2"), make_tweet("1")]
    response = make_page(tweets, cursor="next")
    module = {
        "entryId": "profile-conversation-1",
        "content": {
            "items": [
                {"item": {"itemContent": {"tweet_results": {"result": tweet}}}}
                for tweet in [make_tweet("4"), make_tweet("3")]
            ],
        },
    }
    instructions = response["data"]["user"]["result"]["timeline"]["timeline"][
        "instructions"
    ]
    instructions[1]["entries"].insert(0, module)
    page, cursor = parse_timeline(response)
    assert [tweet["rest_id"] for tweet in page] == ["4", "3", "2", "1"]
    assert cursor == "next"


def test_url_matches_twitter_handler():
    match = re.search(patterns[0], status_url("1234567890"))
    assert match
    assert match.group(1) == "1234567890"


def test_operation_patterns():
    html = (
        '<link rel="preload" as="script" crossorigin="anonymous" '
        'href="https://abs.twimg.com/responsive-web/client-web/main.4f2a9c1e.js" />'
    )
    assert MAIN_JS_PATTERN.search(html).group(0).endswith("main.4f2a9c1e.js")
    main_js = (
        'e.exports={queryId:"abc-123_X",operationName:"UserTweets",'
        'operationType:"query",metadata:{featureSwitches:["feature_a","feature_b"],'
        'fieldToggles:["withArticlePlainText"]}}'
    )
    operation = USER_TWEETS_OPERATION_PATTERN.search(main_js)
    assert operation.group(1) == "abc-123_X"
    assert operation.group(2) == '"feature_a","feature_b"'


class FakeAPI:
    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.cursors = []

    async def user_tweets(self, _user_id, cursor=None):
        self.cursors.append(cursor)
        return self.pages[len(self.cursors) - 1]


def fetch_ids(pages: list[dict], last_id: int | None):
    watcher = RetweetWatcher()
    watcher.api = FakeAPI(pages)
    watcher.user_id = "42"
    tweets, complete = asyncio.run(watcher.fetch_new_tweets(last_id))
    ids = [int(tweet["rest_id"]) for tweet in tweets]
    return ids, watcher.api.cursors, complete


def test_fetch_new_tweets_stops_at_seen():
    pages = [
        make_page([make_tweet("30"), make_tweet("20")], cursor="page2"),
        make_page([make_tweet("15"), make_tweet("10")], cursor="page3"),
        make_page([make_tweet("5")], cursor="page4"),
    ]
    ids, cursors, complete = fetch_ids(pages, last_id=10)
    assert ids == [15, 20, 30]
    assert cursors == [None, "page2"]
    assert complete


def test_fetch_new_tweets_first_run_takes_one_page():
    pages = [make_page([make_tweet("30"), make_tweet("20")], cursor="page2")]
    ids, cursors, complete = fetch_ids(pages, last_id=None)
    assert ids == [20, 30]
    assert cursors == [None]
    assert complete


def test_fetch_new_tweets_pages_until_seen():
    pages = [
        make_page([make_tweet(str(id_))], cursor=f"page{id_}")
        for id_ in range(100, 0, -10)
    ]
    ids, cursors, complete = fetch_ids(pages, last_id=10)
    assert ids == list(range(20, 101, 10))
    assert len(cursors) == len(pages)
    assert complete


def test_fetch_new_tweets_stops_when_cursor_repeats():
    pages = [
        make_page([make_tweet("30")], cursor="page2"),
        make_page([make_tweet("20")], cursor="page2"),
    ]
    ids, cursors, complete = fetch_ids(pages, last_id=10)
    assert ids == [20, 30]
    assert cursors == [None, "page2"]
    assert not complete


def test_fetch_new_tweets_stops_at_max_pages(monkeypatch):
    monkeypatch.setattr(retweet_watcher, "MAX_PAGES", 2)
    pages = [
        make_page([make_tweet(str(id_))], cursor=f"page{id_}") for id_ in (40, 30, 20)
    ]
    ids, cursors, complete = fetch_ids(pages, last_id=10)
    assert ids == [30, 40]
    assert cursors == [None, "page40"]
    assert not complete


def test_wait_for_rate_limit(monkeypatch):
    slept = []

    async def sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(retweet_watcher.asyncio, "sleep", sleep)
    now = time.time()
    reset = int(now) + 100
    WebAPI.rate_limits["UserTweets"] = RateLimit(remaining=10, reset=reset)
    asyncio.run(RetweetWatcher.wait_for_rate_limit("UserTweets"))
    assert slept == []
    WebAPI.rate_limits["UserTweets"] = RateLimit(remaining=1, reset=reset)
    asyncio.run(RetweetWatcher.wait_for_rate_limit("UserTweets"))
    assert len(slept) == 1
    assert 0 < slept[0] <= reset - now + 1
    # Already reset
    WebAPI.rate_limits["UserTweets"] = RateLimit(remaining=0, reset=reset - 200)
    asyncio.run(RetweetWatcher.wait_for_rate_limit("UserTweets"))
    assert len(slept) == 1


def test_rate_limit_is_recorded_by_operation():
    WebAPI._update_rate_limit(
        "https://x.com/i/api/graphql/abc/UserTweets",
        {"x-rate-limit-remaining": "3", "x-rate-limit-reset": "1700000000"},
    )
    assert WebAPI.rate_limits["UserTweets"] == RateLimit(3, 1700000000)


class FakeWatcher(RetweetWatcher):
    def __init__(self, pages: list[dict], last_id: int | None):
        super().__init__()
        self.api = FakeAPI(pages)
        self.user_id = "42"
        self.last_id = last_id
        self.collected = []
        self.errors = {}

    async def load_last_id(self):
        return self.last_id

    async def save_last_id(self, last_id: int):
        self.last_id = last_id

    async def collect(self, url: str):
        self.collected.append(url)
        return self.errors.get(url)


def test_poll_first_run_only_records_position():
    retweet = make_tweet("30", retweet_of=make_tweet("1", media=True))
    watcher = FakeWatcher([make_page([retweet, make_tweet("20")])], last_id=None)
    asyncio.run(watcher.poll())
    assert watcher.collected == []
    assert watcher.last_id == int(retweet["rest_id"])


def test_poll_collects_new_retweets_in_order():
    tweets = [
        make_tweet("40", retweet_of=make_tweet("4", media=True)),
        make_tweet("30", media=True),
        make_tweet("20", retweet_of=make_tweet("2", media=True)),
        make_tweet("10", retweet_of=make_tweet("1", media=True)),
    ]
    watcher = FakeWatcher([make_page(tweets)], last_id=10)
    asyncio.run(watcher.poll())
    assert watcher.collected == [status_url("2"), status_url("4")]
    assert watcher.last_id == int(tweets[0]["rest_id"])


def test_poll_reports_gap_but_still_collects(monkeypatch):
    sent = []

    async def send_message(_chat_id, text):
        sent.append(text)

    monkeypatch.setattr(retweet_watcher.bot, "send_message", send_message)
    monkeypatch.setattr(retweet_watcher, "MAX_PAGES", 1)
    since = datetime(2026, 10, 28, tzinfo=timezone.utc)
    last_id = snowflake_from_datetime(since)
    retweet = make_tweet(str(last_id + 1), retweet_of=make_tweet("1", media=True))
    watcher = FakeWatcher([make_page([retweet], cursor="page2")], last_id=last_id)
    asyncio.run(watcher.poll())
    assert watcher.collected == [status_url("1")]
    assert len(sent) == 1
    assert "/retweet_backfill 2026-10-28T08:00" in sent[0]


def test_poll_empty_timeline_is_an_error():
    watcher = FakeWatcher([make_page([])], last_id=10)
    with pytest.raises(NazurinError):
        asyncio.run(watcher.poll())


def test_poll_error_is_reported_once_and_never_raises(monkeypatch):
    sent = []

    async def send_message(_chat_id, text):
        sent.append(text)
        raise RuntimeError("Telegram is down")

    monkeypatch.setattr(retweet_watcher.bot, "send_message", send_message)
    watcher = RetweetWatcher()
    error = NazurinError("Bad <response>")
    asyncio.run(watcher.on_poll_error(error))
    asyncio.run(watcher.on_poll_error(error))
    assert len(sent) == 1
    assert "Bad &lt;response&gt;" in sent[0]


def test_snowflake_from_datetime():
    moment = datetime(2026, 10, 28, tzinfo=timezone.utc)
    snowflake = snowflake_from_datetime(moment)
    assert (snowflake >> 22) + TWITTER_EPOCH_MS == moment.timestamp() * 1000
    later = snowflake_from_datetime(moment + timedelta(milliseconds=1))
    assert later > snowflake


def test_datetime_from_snowflake():
    moment = datetime(2026, 10, 28, 12, 34, 56, tzinfo=timezone.utc)
    assert datetime_from_snowflake(snowflake_from_datetime(moment)) == moment


def test_parse_since_defaults_to_utc8():
    assert parse_since("2026-10-28") == datetime(2026, 10, 27, 16, tzinfo=timezone.utc)
    assert parse_since("2026-10-28T12:00+00:00") == datetime(
        2026, 10, 28, 12, tzinfo=timezone.utc
    )


def test_backfill_collects_since_and_reports():
    since = datetime(2026, 10, 28, tzinfo=timezone.utc)
    base = snowflake_from_datetime(since)

    def retweet(offset: int, original: str) -> dict:
        return make_tweet(
            str(base + offset), retweet_of=make_tweet(original, media=True)
        )

    pages = [
        make_page([retweet(3, "3"), retweet(2, "2")], cursor="page2"),
        make_page([retweet(1, "1"), retweet(-1, "0")], cursor="page3"),
    ]
    watcher = FakeWatcher(pages, last_id=base + 10)
    watcher.errors = {status_url("2"): "Bad tweet"}
    report = asyncio.run(watcher.backfill(since))
    assert watcher.collected == [status_url("1"), status_url("2"), status_url("3")]
    # Backfilling doesn't move the polling position
    assert watcher.last_id == base + 10
    assert "retweeted media: 3, failed: 1" in report
    assert f"{status_url('2')}: Bad tweet" in report
    assert "Couldn't read" not in report


def test_backfill_reports_unreached_date():
    since = datetime(2026, 10, 28, tzinfo=timezone.utc)
    base = snowflake_from_datetime(since)
    pages = [make_page([make_tweet(str(base + 1))])]
    report = asyncio.run(FakeWatcher(pages, last_id=None).backfill(since))
    assert "Couldn't read the timeline back to" in report

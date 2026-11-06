from enum import Enum

from nazurin.config import env


class TwitterAPI(Enum):
    SYNDICATION = "syndication"
    WEB = "web"


PRIORITY = 5
COLLECTION = "twitter"

with env.prefixed("TWITTER_"):
    API: TwitterAPI = env.enum(
        "API", enum=TwitterAPI, default=TwitterAPI.WEB, by_value=True
    )
    # Auth token for web API
    AUTH_TOKEN: str = env.str("AUTH_TOKEN", default=None)

    with env.prefixed("RETWEET_"):
        # Collect images from the auth token owner's new retweets
        WATCH_RETWEETS: bool = env.bool("WATCH", default=False)
        # Polling interval in seconds
        RETWEET_INTERVAL: int = env.int("INTERVAL", default=300)
        # User ID to watch, detected from the auth token if not set
        RETWEET_USER_ID: str = env.str("USER_ID", default=None)
        # UserTweets GraphQL query ID, discovered from X's main.js if not set
        USER_TWEETS_QUERY_ID: str = env.str("QUERY_ID", default=None)

    with env.prefixed("FILE_"):
        DESTINATION: str = env.str("PATH", default="Twitter")
        FILENAME: str = env.str(
            "NAME",
            default="{id_str}_{index} - {user[name]}({user[id_str]})",
        )

# Twitter

<https://twitter.com/>

## Configuration

### TWITTER_API

:material-lightbulb-on: Optional, defaults to `web`

API to use, `web` or `syndication`. Web API comes from web page, while Syndication API comes from <https://publish.twitter.com>.

Tweets marked as sensitive cannot be fetched through Syndication API, but can be fetched through Web API if [`TWITTER_AUTH_TOKEN`](#twitter_auth_token) is set.

When Web API is unavailable, you may switch to Syndication API.

_Added in v2.4.0_

### TWITTER_AUTH_TOKEN

:material-lightbulb-on: Optional, required to fetch tweets marked as sensitive

Authorization token for Web API.

To get one, follow these steps:

1. Open <https://twitter.com/> in your browser
2. Open Developer Tools (<kbd>F12</kbd>)
3. Navigate to `Application` tab
4. Expand `Cookies` section in the sidebar and click `https://twitter.com`
5. Copy the value of `auth_token` cookie

_Added in v2.4.0_

### TWITTER_RETWEET_WATCH

:material-lightbulb-on: Optional, defaults to `false`, requires [`TWITTER_AUTH_TOKEN`](#twitter_auth_token)

Periodically check the timeline of the auth token owner, and collect new retweets that have images or videos. When a retweeted tweet quotes another tweet, both are collected if they have media. The user's own tweets and quotes are ignored.

On first start, it only records the latest tweet, past retweets are not collected. Errors are reported to `ADMIN_ID`, successful collections are not.

Each poll reads the timeline back to the last seen tweet, up to 50 pages. If it can't get there, e.g. after a long downtime, the missed time range is reported with a `/retweet_backfill` command to collect it. When the rate limit is almost reached, it waits for the limit to reset.

To collect past retweets, send `/retweet_backfill DATE` as the admin, e.g. `/retweet_backfill 2026-10-28`. `DATE` is in UTC+8 unless a time zone is given. Already collected tweets are skipped, and a report is sent when finished.

### TWITTER_RETWEET_INTERVAL

:material-lightbulb-on: Optional, defaults to `300`

Polling interval of [`TWITTER_RETWEET_WATCH`](#twitter_retweet_watch) in seconds.

### TWITTER_RETWEET_USER_ID

:material-lightbulb-on: Optional, detected from [`TWITTER_AUTH_TOKEN`](#twitter_auth_token) by default

Numeric ID of the user whose retweets are watched.

### TWITTER_RETWEET_QUERY_ID

:material-lightbulb-on: Optional, discovered from X web page by default

Query ID of the `UserTweets` GraphQL operation, set it if discovery fails.

## Customizing Storage Path & File Name

For more information, refer to [Customizing Storage Path & File Name](./index.md/#customizing-storage-path--file-name).

### TWITTER_FILE_PATH

:material-lightbulb-on: Optional, defaults to `Twitter`

Storage path for downloaded images or videos.

### TWITTER_FILE_NAME

:material-lightbulb-on: Optional, defaults to `{id_str}_{index} - {user[name]}({user[id_str]})`

File name for downloaded images or videos.

### Available Variables

_Only common used ones are listed._

```json
{
  "lang": "Tweet language, e.g. ja",
  "favorite_count": 4814,
  "possibly_sensitive": false,
  "created_at": "Creation date",
  "id_str": "Tweet ID",
  "user": {
    "id_str": "User ID",
    "name": "User name",
    "screen_name": "User handle"
  },
  "photos": [
    {
      "width": "Width",
      "height": "Height"
    }
  ],
  "filename": "Original filename in twimg.com URL, without extension",
  "index": "Photo or video index in a tweet"
}
```
